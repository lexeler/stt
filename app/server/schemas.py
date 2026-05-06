from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from .models import Job
from .utils import aware, fmt_duration, fmt_eta, fmt_size, utcnow


class JobOut(BaseModel):
    id: str
    filename: str
    source: str
    size_bytes: int
    size_human: str
    duration_sec: float
    duration_human: str
    eta_sec: Optional[float]
    eta_human: Optional[str]
    status: str
    stage: Optional[str]
    progress: float
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    expires_at: Optional[datetime]
    elapsed_sec: float


class JobsList(BaseModel):
    jobs: list[JobOut]
    total: int


def serialize_job(job: Job) -> JobOut:
    started = aware(job.started_at)
    finished = aware(job.finished_at)
    elapsed = 0.0
    if started:
        end = finished if finished else utcnow()
        elapsed = max(0.0, (end - started).total_seconds())
    return JobOut(
        id=job.id,
        filename=job.filename,
        source=job.source,
        size_bytes=job.size_bytes,
        size_human=fmt_size(job.size_bytes),
        duration_sec=job.duration_sec,
        duration_human=fmt_duration(job.duration_sec),
        eta_sec=job.eta_sec,
        eta_human=fmt_eta(job.eta_sec) if job.eta_sec is not None else None,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        error=job.error,
        created_at=aware(job.created_at),
        started_at=started,
        finished_at=finished,
        expires_at=aware(job.expires_at),
        elapsed_sec=elapsed,
    )
