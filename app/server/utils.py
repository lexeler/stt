from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a possibly naive datetime to UTC-aware. Returns None unchanged."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_eta(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"~{s} сек"
    m = s // 60
    rem = s % 60
    if m < 60:
        return f"~{m} мин {rem} сек" if rem else f"~{m} мин"
    h = m // 60
    rm = m % 60
    return f"~{h} ч {rm} мин"


def fmt_size(b: int) -> str:
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    f = float(b)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if f >= 10 or u != "Б" else f"{int(f)} {u}"
        f /= 1024
    return f"{b} Б"


def fmt_duration(sec: float) -> str:
    s = int(round(sec))
    if s < 60:
        return f"{s} сек"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{m}:{rem:02d}"
    h, rm = divmod(m, 60)
    return f"{h}:{rm:02d}:{s % 60:02d}"
