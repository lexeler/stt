from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from .. import worker
from ..config import settings
from ..deps import require_api_token
from ..pipeline import get_duration
from ..ratelimit import limiter

log = logging.getLogger("dictate")

router = APIRouter()

MAX_BYTES = 200 * 1024 * 1024  # 200 MB hard cap on a single sync call


@router.post(
    "/dictate",
    dependencies=[Depends(require_api_token)],
    summary="Low-latency synchronous transcription",
    description=(
        "For short clips (push-to-talk / dictation). Blocks until the model "
        "returns text. Returns `{text, duration}`. Holds the model lock for "
        "the duration of the call — long files will keep other requests "
        "waiting; for those use `POST /api/transcribe` (async queue) instead."
    ),
)
@limiter.limit(settings.rate_dictate_per_ip)
async def dictate(
    request: Request,
    file: UploadFile = File(...),
):
    if not worker.model_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Модель ещё не загружена",
        )

    tmp_id = uuid.uuid4().hex[:12]
    work_root = settings.data_dir / "dictate_tmp" / tmp_id
    work_root.mkdir(parents=True, exist_ok=True)

    safe_ext = (Path(file.filename or "rec").suffix or ".webm")[:8]
    upload_path = work_root / f"input{safe_ext}"

    chunk_timeout = settings.upload_chunk_timeout_sec
    try:
        total = 0
        with open(upload_path, "wb") as f:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        file.read(1024 * 1024), timeout=chunk_timeout
                    )
                except asyncio.TimeoutError:
                    raise HTTPException(
                        status_code=status.HTTP_408_REQUEST_TIMEOUT,
                        detail=f"Слишком медленная загрузка (>{int(chunk_timeout)}с между чанками)",
                    )
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Аудио больше {MAX_BYTES // 1024 // 1024} МБ",
                    )
                f.write(chunk)

        if total < 256:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Запись пустая",
            )

        try:
            duration = get_duration(str(upload_path))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Не удалось прочитать аудио: {exc}",
            )

        if duration <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Запись пустая")

        result = await asyncio.to_thread(
            worker.transcribe_short_sync, str(upload_path), work_root / "work"
        )
        text = result.full_text.strip()
        log.info("dictate ok: audio=%.1fs → text_len=%d", result.duration, len(text))
        return {"text": text, "duration": result.duration}

    finally:
        shutil.rmtree(work_root, ignore_errors=True)
