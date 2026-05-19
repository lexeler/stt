from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .utils import utcnow


class Base(DeclarativeBase):
    pass


class Job(Base):
    """A single transcription request.

    Auth is server-wide Bearer token (no per-user ownership) — a job is
    identified solely by its id.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    eta_sec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    upload_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    clean_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    ts_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("idx_jobs_created", "created_at"),
        Index("idx_jobs_expires", "expires_at"),
    )
