"""Local FastAPI web app: upload audio/video → GigaAM transcription → download txt."""
from __future__ import annotations

import logging
import queue
import secrets
import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import gigaam
import torch
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("server")

USERNAME = "admin"
PASSWORD = "lemoon"
SESSION_COOKIE = "session"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Citrus Lab — Расшифровщик")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

sessions: set[str] = set()
sessions_lock = threading.Lock()


@dataclass
class Job:
    id: str
    filename: str
    bytes: int
    duration: float
    eta_sec: float
    created_at: float = field(default_factory=time.time)
    status: str = "queued"  # queued, running, done, failed, cancelled
    progress: float = 0.0
    stage: str = "В очереди"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    full_text: Optional[str] = None
    timestamped_text: Optional[str] = None
    upload_path: Path = field(default_factory=Path)
    work_dir: Path = field(default_factory=Path)
    cancel_requested: bool = False


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
job_queue: "queue.Queue[str]" = queue.Queue()
model_holder: dict = {"model": None}

# Adaptive ETA — rolling window of (real_processing_seconds / audio_duration_seconds)
recent_ratios: deque[float] = deque(maxlen=5)
DEFAULT_RATIO = 0.18 if torch.cuda.is_available() else 0.7
DEFAULT_OVERHEAD = 5.0 if torch.cuda.is_available() else 8.0


def estimate_eta(duration: float) -> float:
    if recent_ratios:
        ratio = sum(recent_ratios) / len(recent_ratios)
    else:
        ratio = DEFAULT_RATIO
    return max(DEFAULT_OVERHEAD, duration * ratio + DEFAULT_OVERHEAD)


def is_authed(request: Request) -> bool:
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return False
    with sessions_lock:
        return tok in sessions


def fmt_eta(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"~{s} сек"
    m = s // 60
    rem = s % 60
    if m < 60:
        return f"~{m} мин {rem} сек" if rem else f"~{m} мин"
    h = m // 60
    rm = m % 60
    return f"~{h} ч {rm} мин"


def fmt_size(b: int) -> str:
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    f = float(b)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if f >= 10 or u != "Б" else f"{int(f)} {u}"
        f /= 1024
    return f"{b} Б"


def fmt_duration(sec: float) -> str:
    s = int(round(sec))
    if s < 60:
        return f"{s} сек"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{m}:{rem:02d}"
    h, rm = divmod(m, 60)
    return f"{h}:{rm:02d}:{s % 60:02d}"


def serialize_job(job: Job) -> dict:
    elapsed = 0.0
    if job.started_at:
        end = job.finished_at if job.finished_at else time.time()
        elapsed = max(0.0, end - job.started_at)
    return {
        "id": job.id,
        "filename": job.filename,
        "size_bytes": job.bytes,
        "size_human": fmt_size(job.bytes),
        "duration_sec": job.duration,
        "duration_human": fmt_duration(job.duration),
        "eta_sec": job.eta_sec,
        "eta_human": fmt_eta(job.eta_sec),
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "created_at": job.created_at,
        "elapsed_sec": elapsed,
        "error": job.error,
    }


def cleanup_job_files(job: Job) -> None:
    """Remove the job's directory (uploads + work)."""
    job_dir = UPLOADS_DIR / job.id
    shutil.rmtree(job_dir, ignore_errors=True)


def worker_loop() -> None:
    log.info("Worker thread started")
    from pipeline import transcribe_audio, CancelledByUser
    while True:
        job_id = job_queue.get()
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                continue
            if job.cancel_requested:
                job.status = "cancelled"
                job.stage = "Отменено"
                job.finished_at = time.time()
                cleanup_job_files(job)
                continue
            job.status = "running"
            job.started_at = time.time()
            job.stage = "Запуск"

        def on_progress(frac: float, stage: str) -> None:
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.progress = frac
                    j.stage = stage

        def should_cancel() -> bool:
            with jobs_lock:
                j = jobs.get(job_id)
                return bool(j and j.cancel_requested)

        try:
            result = transcribe_audio(
                model_holder["model"],
                str(job.upload_path),
                job.work_dir,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )

            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.full_text = result.render_clean()
                    j.timestamped_text = result.render_timestamped()
                    j.status = "done"
                    j.progress = 1.0
                    j.stage = "Готово"
                    j.finished_at = time.time()
                    j.duration = result.duration
                    if j.started_at and result.duration > 0:
                        ratio = (j.finished_at - j.started_at) / result.duration
                        recent_ratios.append(ratio)
                        log.info(
                            "Job %s done in %.1fs (ratio=%.2f, avg=%.2f)",
                            job_id, j.finished_at - j.started_at, ratio,
                            sum(recent_ratios) / len(recent_ratios),
                        )

        except CancelledByUser:
            log.info("Job %s cancelled by user", job_id)
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.status = "cancelled"
                    j.stage = "Отменено"
                    j.finished_at = time.time()
            cleanup_job_files(job)

        except Exception as e:
            log.exception("Job %s failed", job_id)
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.status = "failed"
                    j.error = str(e)
                    j.stage = "Ошибка"
                    j.finished_at = time.time()


@app.on_event("startup")
def on_startup() -> None:
    log.info("Loading GigaAM v3 (this happens once)...")
    model_holder["model"] = gigaam.load_model("v3_e2e_rnnt")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info("Model ready on %s. Default ETA ratio=%.2f. Starting worker.", device, DEFAULT_RATIO)
    threading.Thread(target=worker_loop, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if is_authed(request):
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None):
    if is_authed(request):
        return RedirectResponse("/app", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username.strip() == USERNAME and password == PASSWORD:
        token = secrets.token_urlsafe(32)
        with sessions_lock:
            sessions.add(token)
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, token,
            httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/",
        )
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        with sessions_lock:
            sessions.discard(tok)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "app.html", {})


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not is_authed(request):
        raise HTTPException(401, "Unauthorized")

    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOADS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(file.filename or "audio").name or "audio"
    upload_path = job_dir / safe_name

    total = 0
    try:
        with open(upload_path, "wb") as f:
            while True:
                chunk = await file.read(8 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    f.close()
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(413, "Файл больше 10 ГБ")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        log.exception("Upload write failed")
        raise HTTPException(500, f"Ошибка сохранения файла: {e}")

    try:
        from pipeline import get_duration
        duration = get_duration(str(upload_path))
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"Не удалось прочитать аудио из файла: {e}")

    eta = estimate_eta(duration)
    work_dir = job_dir / "work"

    job = Job(
        id=job_id,
        filename=safe_name,
        bytes=total,
        duration=duration,
        eta_sec=eta,
        upload_path=upload_path,
        work_dir=work_dir,
    )
    with jobs_lock:
        jobs[job_id] = job
    job_queue.put(job_id)
    log.info("Queued job %s file=%s duration=%.1fs eta=%.1fs", job_id, safe_name, duration, eta)

    return JSONResponse(serialize_job(job))


@app.get("/api/jobs")
def list_jobs(request: Request):
    if not is_authed(request):
        raise HTTPException(401)
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: j.created_at, reverse=True)
        return {"jobs": [serialize_job(j) for j in items]}


@app.get("/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    if not is_authed(request):
        raise HTTPException(401)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        return serialize_job(job)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str):
    if not is_authed(request):
        raise HTTPException(401)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        if job.status in ("done", "failed", "cancelled"):
            return JSONResponse(serialize_job(job))
        job.cancel_requested = True
        if job.status == "queued":
            job.stage = "Отмена…"
        else:
            job.stage = "Отменяется…"
    log.info("Cancel requested for job %s", job_id)
    return JSONResponse(serialize_job(job))


@app.delete("/jobs/{job_id}")
def job_delete(request: Request, job_id: str):
    if not is_authed(request):
        raise HTTPException(401)
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        if job.status not in ("done", "failed", "cancelled"):
            raise HTTPException(400, "Нельзя удалять активную задачу — сначала отмени")
        del jobs[job_id]
    cleanup_job_files(job)
    return {"ok": True}


@app.get("/jobs/{job_id}/text/{kind}")
def job_text(request: Request, job_id: str, kind: str):
    if not is_authed(request):
        raise HTTPException(401)
    if kind not in ("clean", "timestamps"):
        raise HTTPException(400, "kind must be 'clean' or 'timestamps'")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.status != "done":
            raise HTTPException(404)
        text = job.full_text if kind == "clean" else job.timestamped_text
    if not text:
        raise HTTPException(404)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(text)


@app.get("/jobs/{job_id}/download/{kind}")
def job_download(request: Request, job_id: str, kind: str):
    if not is_authed(request):
        raise HTTPException(401)
    if kind not in ("clean", "timestamps"):
        raise HTTPException(400, "kind must be 'clean' or 'timestamps'")

    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.status != "done":
            raise HTTPException(404)
        text = job.full_text if kind == "clean" else job.timestamped_text
        base = Path(job.filename).stem
        suffix = "" if kind == "clean" else "_timestamps"
        download_name = f"{base}{suffix}.txt"

    if not text:
        raise HTTPException(404)

    out_dir = UPLOADS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"transcript_{kind}.txt"
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(
        out_path,
        media_type="text/plain; charset=utf-8",
        filename=download_name,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
