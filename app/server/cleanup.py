from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import Job
from .utils import aware, utcnow

log = logging.getLogger("cleanup")

CLEANUP_INTERVAL_SEC = 60 * 60
STALE_CHECK_INTERVAL_SEC = 15 * 60
LOOP_WAKE_SEC = 5 * 60


def _delete_transcript_dir(job: Job) -> None:
    paths_to_remove = set()
    for p in (job.clean_path, job.ts_path):
        if not p:
            continue
        path = Path(p)
        if path.exists():
            paths_to_remove.add(path)
        if path.parent.exists():
            paths_to_remove.add(path.parent)
    for p in sorted(paths_to_remove, key=lambda x: -len(str(x))):
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                if not any(p.iterdir()):
                    p.rmdir()
        except OSError:
            pass


def _purge_expired_jobs() -> int:
    now = utcnow()
    removed = 0
    with SessionLocal() as db:
        expired = db.scalars(
            select(Job).where(Job.expires_at.is_not(None), Job.expires_at < now)
        ).all()
        for job in expired:
            _delete_transcript_dir(job)
            if job.upload_path:
                shutil.rmtree(Path(job.upload_path).parent, ignore_errors=True)
            db.delete(job)
            removed += 1
        if expired:
            db.commit()
    return removed


def _purge_orphan_uploads() -> int:
    """Files in uploads/<job_id>/ where the job no longer exists or is done/failed."""
    if not settings.uploads_dir.exists():
        return 0
    removed = 0
    with SessionLocal() as db:
        existing_ids = {jid for (jid,) in db.execute(select(Job.id)).all()}
        for child in settings.uploads_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name not in existing_ids:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed


def _purge_orphan_transcripts() -> int:
    """Transcript dirs whose job_id doesn't exist anymore."""
    if not settings.transcripts_dir.exists():
        return 0
    removed = 0
    with SessionLocal() as db:
        existing_ids = {jid for (jid,) in db.execute(select(Job.id)).all()}
        for job_dir in settings.transcripts_dir.iterdir():
            if not job_dir.is_dir():
                continue
            if job_dir.name not in existing_ids:
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
    return removed


def _mark_stale_running() -> int:
    """Running jobs whose started_at is older than threshold → failed."""
    threshold = utcnow() - timedelta(hours=settings.stale_running_hours)
    with SessionLocal() as db:
        stale = db.scalars(
            select(Job).where(
                Job.status == "running",
                Job.started_at.is_not(None),
                Job.started_at < threshold,
            )
        ).all()
        for j in stale:
            j.status = "failed"
            j.stage = "Ошибка"
            j.error = "Превышено время обработки"
            j.finished_at = utcnow()
        if stale:
            db.commit()
        return len(stale)


def run_cleanup_once() -> dict:
    stats = {
        "expired_jobs": _purge_expired_jobs(),
        "orphan_uploads": _purge_orphan_uploads(),
        "orphan_transcripts": _purge_orphan_transcripts(),
    }
    if any(stats.values()):
        log.info("Cleanup: %s", stats)
    return stats


def run_stale_check_once() -> int:
    n = _mark_stale_running()
    if n:
        log.info("Marked %d stale running jobs as failed", n)
    return n


async def cleanup_loop() -> None:
    """Long-running background task. Runs cleanup hourly + stale-check every 5m."""
    last_cleanup = 0.0
    last_stale = 0.0
    while True:
        loop_t = asyncio.get_event_loop().time()
        try:
            if loop_t - last_cleanup >= CLEANUP_INTERVAL_SEC:
                await asyncio.to_thread(run_cleanup_once)
                last_cleanup = loop_t
            if loop_t - last_stale >= STALE_CHECK_INTERVAL_SEC:
                await asyncio.to_thread(run_stale_check_once)
                last_stale = loop_t
        except Exception:
            log.exception("Cleanup loop tick failed")
        await asyncio.sleep(LOOP_WAKE_SEC)
