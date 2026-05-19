from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as ORMSession

from ..config import settings
from ..db import get_db
from ..deps import require_api_token
from ..models import Job
from ..ratelimit import limiter
from ..schemas import JobOut, JobsList, serialize_job

log = logging.getLogger("jobs")

router = APIRouter(dependencies=[Depends(require_api_token)])


def _load(db: ORMSession, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Задача не найдена")
    return job


@router.get(
    "/jobs",
    response_model=JobsList,
    summary="List jobs",
    description="Return all jobs known to the server, newest first.",
)
@limiter.limit(settings.rate_read_per_ip)
def list_jobs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: ORMSession = Depends(get_db),
):
    total = db.scalar(select(func.count()).select_from(Job)) or 0
    jobs = db.scalars(
        select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return JobsList(jobs=[serialize_job(j) for j in jobs], total=total)


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    summary="Get job status",
)
@limiter.limit(settings.rate_read_per_ip)
def get_job(request: Request, job_id: str, db: ORMSession = Depends(get_db)):
    return serialize_job(_load(db, job_id))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobOut,
    summary="Cancel a queued or running job",
)
@limiter.limit(settings.rate_read_per_ip)
def cancel_job(request: Request, job_id: str, db: ORMSession = Depends(get_db)):
    job = _load(db, job_id)
    if job.status in ("done", "failed", "cancelled"):
        return serialize_job(job)
    job.cancel_requested = True
    if job.status == "queued":
        job.stage = "Отмена…"
    else:
        job.stage = "Отменяется…"
    db.commit()
    db.refresh(job)
    log.info("Cancel requested for %s", job_id)
    return serialize_job(job)


def _wipe_job_files(job: Job) -> None:
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


@router.delete("/jobs/{job_id}", summary="Delete a finished job and its artefacts")
@limiter.limit(settings.rate_read_per_ip)
def delete_job(request: Request, job_id: str, db: ORMSession = Depends(get_db)):
    job = _load(db, job_id)
    if job.status not in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя удалить активную задачу — сначала отмените её",
        )
    _wipe_job_files(job)
    db.delete(job)
    db.commit()
    return {"ok": True}


@router.delete("/jobs", summary="Wipe every finished job from the server")
@limiter.limit(settings.rate_read_per_ip)
def delete_all_jobs(request: Request, db: ORMSession = Depends(get_db)):
    all_jobs = db.scalars(select(Job)).all()
    removed = 0
    skipped_active = 0
    for job in all_jobs:
        if job.status in ("queued", "running"):
            skipped_active += 1
            continue
        _wipe_job_files(job)
        db.delete(job)
        removed += 1
    db.commit()
    return {"ok": True, "removed": removed, "skipped_active": skipped_active}
