from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as ORMSession

from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import require_api_token
from ..models import Job
from ..ratelimit import limiter
from ..utils import fmt_duration

log = logging.getLogger("transcripts")

router = APIRouter(dependencies=[Depends(require_api_token)])


def _wipe_job_artifacts(job_id: str) -> None:
    """Delete all on-disk artefacts and the job row itself.
    Runs as a background task right after the result has been delivered to
    the client — the server retains nothing about the audio after that.
    """
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        for p in (job.clean_path, job.ts_path):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        for p in (job.clean_path, job.ts_path):
            if p:
                parent = Path(p).parent
                try:
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError:
                    pass
        if job.upload_path:
            shutil.rmtree(Path(job.upload_path).parent, ignore_errors=True)
        shutil.rmtree(settings.uploads_dir / job.id, ignore_errors=True)
        db.delete(job)
        db.commit()
    log.info("auto-wiped %s after result fetch", job_id)


@router.get(
    "/jobs/{job_id}/result",
    summary="Fetch finished transcript (auto-deletes the job)",
    description=(
        "Returns both the clean transcript and a timestamped version. "
        "The server deletes the job and all artefacts immediately after "
        "this call — call once and store the result on your side."
    ),
)
@limiter.limit(settings.rate_read_per_ip)
def fetch_result(
    request: Request,
    job_id: str,
    background_tasks: BackgroundTasks,
    db: ORMSession = Depends(get_db),
):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")

    if job.status != "done":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Текст ещё не готов",
        )
    if not job.clean_path or not job.ts_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Текст не найден",
        )

    clean_path = Path(job.clean_path)
    ts_path = Path(job.ts_path)

    if not clean_path.exists() or not ts_path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Текст уже удалён",
        )

    clean_text = clean_path.read_text(encoding="utf-8")
    ts_text = ts_path.read_text(encoding="utf-8")

    background_tasks.add_task(_wipe_job_artifacts, job.id)

    return {
        "id": job.id,
        "filename": job.filename,
        "source": job.source,
        "duration_sec": job.duration_sec,
        "duration_human": fmt_duration(job.duration_sec),
        "clean": clean_text,
        "timestamps": ts_text,
    }
