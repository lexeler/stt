from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session as ORMSession

from .. import worker
from ..config import settings
from ..db import get_db
from ..deps import require_api_token
from ..models import Job
from ..pipeline import get_duration
from ..ratelimit import enforce_global_quota, limiter
from ..schemas import JobOut, serialize_job
from ..utils import utcnow

log = logging.getLogger("transcribe")

router = APIRouter()


def _detect_source(content_type: str | None, filename: str) -> str:
    if filename.lower().startswith("recording-"):
        return "mic"
    if content_type and ("webm" in content_type.lower() or "ogg" in content_type.lower()):
        return "mic"
    return "upload"


def _is_audio_or_video(content_type: str | None, filename: str) -> bool:
    if content_type:
        ct = content_type.lower()
        if ct.startswith("audio/") or ct.startswith("video/"):
            return True
        if ct in ("application/octet-stream", "application/ogg"):
            return True
    if filename:
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext in {
            "mp3", "wav", "m4a", "ogg", "oga", "opus", "flac", "aac", "wma",
            "webm", "mp4", "m4v", "mkv", "mov", "avi", "3gp", "amr",
        }:
            return True
    return False


async def _enqueue_upload(
    request: Request,
    file: UploadFile,
    db: ORMSession,
) -> JobOut:
    enforce_global_quota()

    # Reject when the worker is swamped — prevents callers from filling the
    # queue with thousands of tiny jobs and bloating SQLite/disk.
    if worker.queue_depth() >= settings.max_queue_depth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Сервер занят: очередь полна ({settings.max_queue_depth} jobs). Повтори позже.",
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
                        detail=f"Слишком медленная загрузка (>{int(chunk_timeout)}с между чанками)",
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
        log.exception("Upload write failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ошибка сохранения файла: {exc}",
        )

    if total < 256:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Файл пустой или слишком маленький",
        )

    try:
        duration = get_duration(str(upload_path))
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось прочитать аудио из файла: {exc}",
        )

    if duration <= 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Аудио пустое")

    if duration > settings.max_duration_sec:
        shutil.rmtree(job_dir, ignore_errors=True)
        max_h = settings.max_duration_sec / 3600
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Аудио длиннее {max_h:.1f} ч ({duration / 3600:.1f} ч)",
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
    log.info("Queued %s file=%s duration=%.1fs eta=%.1fs", job_id, safe_name, duration, eta)
    return serialize_job(job)


@router.post(
    "/transcribe",
    response_model=JobOut,
    dependencies=[Depends(require_api_token)],
    summary="Submit audio for async transcription",
    description=(
        "Upload an audio or video file (multipart form field `file`). "
        "Returns a `JobOut` with `id` to poll. Job is processed asynchronously "
        "with silence-aware chunking for long recordings. Poll "
        "`GET /api/jobs/{id}` then call `GET /api/jobs/{id}/result` once `status=done`."
    ),
)
@limiter.limit(settings.rate_per_ip)
async def submit_transcribe(
    request: Request,
    file: UploadFile = File(...),
    db: ORMSession = Depends(get_db),
) -> JobOut:
    return await _enqueue_upload(request, file, db)


@router.post(
    "/upload",
    response_model=JobOut,
    dependencies=[Depends(require_api_token)],
    deprecated=True,
    summary="[Deprecated] Alias for POST /api/transcribe",
)
@limiter.limit(settings.rate_per_ip)
async def submit_upload_alias(
    request: Request,
    file: UploadFile = File(...),
    db: ORMSession = Depends(get_db),
) -> JobOut:
    return await _enqueue_upload(request, file, db)
