from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from . import worker
from .config import settings
from .db import engine
from .models import Base
from .ratelimit import limiter
from .routers import dictate as dictate_router
from .routers import health as health_router
from .routers import jobs as jobs_router
from .routers import transcribe as transcribe_router
from .routers import transcripts as transcripts_router
from .routers import webui as webui_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


def _ensure_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.transcripts_dir.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_dirs()
    Base.metadata.create_all(bind=engine)

    if settings.asr_load_on_startup:
        await asyncio.to_thread(worker.preload_model)
    else:
        log.warning("ASR_LOAD_ON_STARTUP=false — model NOT loaded; jobs will fail until set_model().")

    worker.recover_after_restart()
    worker.start_worker_thread()

    from . import cleanup
    cleanup_task = asyncio.create_task(cleanup.cleanup_loop())

    log.info("Startup complete (debug=%s, api_token=%s)",
             settings.debug, "set" if settings.api_token else "MISSING")
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except (asyncio.CancelledError, Exception):
            pass


API_DESCRIPTION = """
Texpin Speech-to-Text API.

GigaAM v3 RNN-T transcription with automatic silence-aware chunking for long
recordings. All endpoints under `/api/*` require Bearer authentication using
the `API_TOKEN` from server `.env`.

## Workflow

1. **POST `/api/transcribe`** — upload audio (multipart `file`), get a `job_id`.
2. **GET `/api/jobs/{job_id}`** — poll status (`queued` → `running` → `done`).
3. **GET `/api/jobs/{job_id}/result`** — fetch transcript. The job and all
   artefacts are deleted server-side right after this call.

For low-latency dictation (short clips, blocking call) use **POST `/api/dictate`**
— same auth, returns `{text, duration}` synchronously.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Texpin STT API",
        description=API_DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _rl(_request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests from this IP, slow down."},
        )

    app.include_router(webui_router.router, tags=["webui"])
    app.include_router(transcribe_router.router, prefix="/api", tags=["transcribe"])
    app.include_router(jobs_router.router, prefix="/api", tags=["jobs"])
    app.include_router(transcripts_router.router, prefix="/api", tags=["transcripts"])
    app.include_router(dictate_router.router, prefix="/api", tags=["dictate"])
    app.include_router(health_router.router, tags=["health"])

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, log_level="info")
