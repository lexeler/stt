"""Browser cabinet at GET / — HTTP Basic auth, drag-drop upload, polling.

The /web/* endpoints reuse the same worker/queue as the Bearer /api/*; only
the auth scheme and the rate-limit budget differ. Keeps API callers and the
human-in-a-browser case isolated so one can't exhaust the other's limits.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session as ORMSession

from .. import worker
from ..config import settings
from ..db import get_db
from ..deps import require_web_basic_auth
from ..models import Job
from ..pipeline import get_duration
from ..ratelimit import enforce_global_quota, limiter
from ..schemas import JobOut, serialize_job
from ..utils import fmt_duration, utcnow
from .transcribe import _is_audio_or_video, _detect_source
from .transcripts import _wipe_job_artifacts

log = logging.getLogger("webui")

router = APIRouter(dependencies=[Depends(require_web_basic_auth)])

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit(settings.rate_web_per_ip)
async def cabinet_root(request: Request):
    """The single-page cabinet — drag-drop uploader with status polling."""
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "web/index.html missing")
    return FileResponse(index, media_type="text/html; charset=utf-8")


@router.post("/web/upload", response_model=JobOut, include_in_schema=False)
@limiter.limit(settings.rate_web_per_ip)
async def web_upload(
    request: Request,
    file: UploadFile = File(...),
    db: ORMSession = Depends(get_db),
) -> JobOut:
    enforce_global_quota()

    if worker.queue_depth() >= settings.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Сервер занят: очередь полна ({settings.max_queue_depth} jobs).",
        )

    safe_name = Path(file.filename or "audio").name or "audio"
    if not _is_audio_or_video(file.content_type, safe_name):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Поддерживаются только аудио и видеофайлы",
        )

    job_id = uuid.uuid4().hex[:12]
    job_dir = settings.uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    upload_path = job_dir / safe_name

    chunk_timeout = settings.upload_chunk_timeout_sec
    total = 0
    try:
        with open(upload_path, "wb") as f:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        file.read(8 * 1024 * 1024), timeout=chunk_timeout
                    )
                except asyncio.TimeoutError:
                    f.close()
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_408_REQUEST_TIMEOUT,
                        detail=f"Слишком медленная загрузка (>{int(chunk_timeout)}с)",
                    )
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    f.close()
                    shutil.rmtree(job_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Файл больше {settings.max_upload_bytes // (1024 ** 3)} ГБ",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        log.exception("upload write failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка сохранения файла: {exc}",
        )

    if total < 256:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Файл пустой")

    try:
        duration = get_duration(str(upload_path))
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось прочитать аудио: {exc}",
        )
    if duration <= 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Аудио пустое")
    if duration > settings.max_duration_sec:
        shutil.rmtree(job_dir, ignore_errors=True)
        max_h = settings.max_duration_sec / 3600
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Аудио длиннее {max_h:.1f} ч",
        )

    eta = worker.estimate_eta(duration)
    job = Job(
        id=job_id,
        filename=safe_name,
        source=_detect_source(file.content_type, safe_name),
        size_bytes=total,
        duration_sec=duration,
        status="queued",
        stage="В очереди",
        progress=0.0,
        eta_sec=eta,
        created_at=utcnow(),
        upload_path=str(upload_path),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    worker.enqueue_job(job_id)
    log.info("web upload %s %s %.1fs", job_id, safe_name, duration)
    return serialize_job(job)


@router.get("/web/jobs/{job_id}", response_model=JobOut, include_in_schema=False)
@limiter.limit(settings.rate_web_per_ip)
def web_status(request: Request, job_id: str, db: ORMSession = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return serialize_job(job)


@router.get("/web/jobs/{job_id}/result", include_in_schema=False)
@limiter.limit(settings.rate_web_per_ip)
def web_result(
    request: Request,
    job_id: str,
    background_tasks: BackgroundTasks,
    db: ORMSession = Depends(get_db),
):
    """Returns clean text and timestamps in one shot; deletes the job in the
    background as soon as the response is sent."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    if job.status != "done":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Текст ещё не готов")
    if not job.clean_path or not job.ts_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Текст не найден")

    clean = Path(job.clean_path)
    ts = Path(job.ts_path)
    if not clean.exists() or not ts.exists():
        raise HTTPException(status.HTTP_410_GONE, "Текст уже удалён")

    payload = {
        "id": job.id,
        "filename": job.filename,
        "duration_sec": job.duration_sec,
        "duration_human": fmt_duration(job.duration_sec),
        "clean": clean.read_text(encoding="utf-8"),
        "timestamps": ts.read_text(encoding="utf-8"),
    }
    background_tasks.add_task(_wipe_job_artifacts, job.id)
    return payload
