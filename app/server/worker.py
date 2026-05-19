from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from . import pipeline as ASR
from .config import settings
from .db import SessionLocal
from .models import Job
from .utils import aware, utcnow

log = logging.getLogger("worker")

_job_queue: "queue.Queue[str]" = queue.Queue()
_model = None
_recent_ratios: deque[float] = deque(maxlen=20)
_state_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None

# GigaAM model is not safe to call concurrently. The queue worker and the
# synchronous dictate endpoint both need this lock around any model.transcribe().
_model_use_lock = threading.Lock()


def _load_persisted_ratios() -> None:
    p = settings.eta_state_path
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
        for x in data.get("ratios", []):
            _recent_ratios.append(float(x))
    except Exception:
        log.warning("Could not read %s", p, exc_info=True)


def _persist_ratios() -> None:
    p = settings.eta_state_path
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ratios": list(_recent_ratios)}))
    except Exception:
        log.warning("Could not persist ETA state", exc_info=True)


def _has_cuda() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def estimate_eta(duration: float) -> float:
    with _state_lock:
        ratios = list(_recent_ratios)
    if ratios:
        ratio = sum(ratios) / len(ratios)
    else:
        ratio = settings.cuda_default_ratio if _has_cuda() else settings.cpu_default_ratio
    overhead = settings.cuda_default_overhead if _has_cuda() else settings.cpu_default_overhead
    return max(overhead, duration * ratio + overhead)


def enqueue_job(job_id: str) -> None:
    _job_queue.put(job_id)


def queue_depth() -> int:
    return _job_queue.qsize()


def model_loaded() -> bool:
    return _model is not None


def set_model(m) -> None:
    """Used by tests to inject a fake model."""
    global _model
    _model = m


def preload_model() -> None:
    global _model
    import gigaam
    log.info("Loading GigaAM %s ...", settings.asr_model)
    _model = gigaam.load_model(settings.asr_model)
    log.info("Model ready (cuda=%s)", _has_cuda())


def transcribe_short_sync(audio_path: str, work_dir: Path):
    """Synchronous single-shot transcription for the dictate endpoint.

    Blocks if the queue worker is currently using the model. Returns a
    pipeline.TranscriptResult; the caller reads .full_text.
    """
    if _model is None:
        raise RuntimeError("Модель ещё не загружена")
    with _model_use_lock:
        return ASR.transcribe_audio(_model, audio_path, work_dir)


def recover_after_restart() -> None:
    """Mark stale running jobs as failed; re-queue queued jobs."""
    with SessionLocal() as db:
        running = db.scalars(select(Job).where(Job.status == "running")).all()
        for j in running:
            j.status = "failed"
            j.stage = "Ошибка"
            j.error = "Сервер перезапущен во время обработки"
            j.finished_at = utcnow()
        if running:
            log.info("Recovery: marked %d running jobs as failed", len(running))

        queued = db.scalars(select(Job).where(Job.status == "queued")).all()
        db.commit()
        for j in queued:
            _job_queue.put(j.id)
        if queued:
            log.info("Recovery: re-queued %d jobs", len(queued))


def start_worker_thread() -> None:
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _load_persisted_ratios()
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="asr-worker")
    _worker_thread.start()


def _transcript_dir(job_id: str) -> Path:
    return settings.transcripts_dir / job_id


def _retention() -> timedelta:
    return timedelta(minutes=settings.job_retention_minutes)


def _cleanup_upload_dir(job_id: str) -> None:
    shutil.rmtree(settings.uploads_dir / job_id, ignore_errors=True)


def _worker_loop() -> None:
    log.info("Worker thread started")
    while True:
        job_id = _job_queue.get()
        try:
            _process(job_id)
        except Exception:
            log.exception("Unhandled exception while processing %s", job_id)


def _process(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        if job.cancel_requested:
            job.status = "cancelled"
            job.stage = "Отменено"
            job.finished_at = utcnow()
            db.commit()
            _cleanup_upload_dir(job_id)
            return
        job.status = "running"
        job.started_at = utcnow()
        job.stage = "Запуск"
        db.commit()

        upload_path = job.upload_path

    if not upload_path or not Path(upload_path).exists():
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            if j:
                j.status = "failed"
                j.stage = "Ошибка"
                j.error = "Загруженный файл не найден"
                j.finished_at = utcnow()
                db.commit()
        return

    work_dir = settings.uploads_dir / job_id / "work"

    def on_progress(frac: float, stage: str) -> None:
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            if j:
                j.progress = float(frac)
                j.stage = stage
                db.commit()

    def should_cancel() -> bool:
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            return bool(j and j.cancel_requested)

    try:
        with _model_use_lock:
            result = ASR.transcribe_audio(
                _model,
                upload_path,
                work_dir,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )

        out_dir = _transcript_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        clean_path = out_dir / "clean.txt"
        ts_path = out_dir / "timestamps.txt"
        clean_path.write_text(result.render_clean(), encoding="utf-8")
        ts_path.write_text(result.render_timestamped(), encoding="utf-8")

        finished = utcnow()
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            if j:
                j.status = "done"
                j.progress = 1.0
                j.stage = "Готово"
                j.finished_at = finished
                j.duration_sec = result.duration
                j.clean_path = str(clean_path)
                j.ts_path = str(ts_path)
                j.expires_at = finished + _retention()
                started = aware(j.started_at)
                if started and result.duration > 0:
                    elapsed = (finished - started).total_seconds()
                    ratio = elapsed / result.duration
                    with _state_lock:
                        _recent_ratios.append(ratio)
                    _persist_ratios()
                    log.info(
                        "Job %s done in %.1fs (ratio=%.3f, n=%d)",
                        job_id, elapsed, ratio, len(_recent_ratios),
                    )
                j.upload_path = None
                db.commit()
        _cleanup_upload_dir(job_id)

    except ASR.CancelledByUser:
        log.info("Job %s cancelled by user", job_id)
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            if j:
                j.status = "cancelled"
                j.stage = "Отменено"
                j.finished_at = utcnow()
                j.upload_path = None
                db.commit()
        _cleanup_upload_dir(job_id)

    except Exception as exc:
        log.exception("Job %s failed", job_id)
        with SessionLocal() as db:
            j = db.get(Job, job_id)
            if j:
                j.status = "failed"
                j.stage = "Ошибка"
                j.error = str(exc)[:1000]
                j.finished_at = utcnow()
                j.upload_path = None
                db.commit()
        _cleanup_upload_dir(job_id)
