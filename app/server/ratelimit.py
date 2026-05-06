"""
Rate limiting for upload endpoint.

Two layers:
  1. Per-IP throttle via slowapi — limits a single source from spamming.
     Default 40 uploads/hour. Lives in slowapi's in-memory storage.
  2. Global server cap — total uploads across all clients in a sliding hour
     window. Default 100/hour. Protects the box itself: each transcription
     is real CPU time, and we don't want to be DoS'd by ten different IPs
     each hitting their own personal limit.

Both windows are sliding and reset naturally as old entries fall off.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import settings


def _real_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_real_ip)


# ---- Global hourly cap ----------------------------------------------------

_global_lock = threading.Lock()
_recent: deque[float] = deque()


def enforce_global_quota() -> None:
    now = time.time()
    window = settings.global_window_sec
    with _global_lock:
        # drop entries older than the window
        cutoff = now - window
        while _recent and _recent[0] < cutoff:
            _recent.popleft()
        if len(_recent) >= settings.global_limit:
            wait_min = max(1, int((window - (now - _recent[0])) / 60))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Сервис временно перегружен. "
                    f"Попробуйте через ~{wait_min} мин."
                ),
            )
        _recent.append(now)


__all__ = ["limiter", "enforce_global_quota", "RateLimitExceeded"]
