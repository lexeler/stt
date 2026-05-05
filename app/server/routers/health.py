from __future__ import annotations

from fastapi import APIRouter

from .. import worker

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "model_loaded": worker.model_loaded(),
        "queue_depth": worker.queue_depth(),
    }
