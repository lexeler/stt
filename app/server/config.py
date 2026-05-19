from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = APP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[APP_DIR / ".env", PROJECT_DIR / ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    debug: bool = False

    data_dir: Path = APP_DIR / "data"
    uploads_dir: Path = APP_DIR / "data" / "uploads"
    transcripts_dir: Path = APP_DIR / "data" / "transcripts"
    db_path: Path = APP_DIR / "data" / "app.db"
    eta_state_path: Path = APP_DIR / "data" / "eta.json"

    # Hard server-side guardrails. NOT user-tier features — anyone can hit them.
    # Required to keep the box from being weaponised by a bad upload.
    max_upload_bytes: int = 2 * 1024 ** 3
    max_duration_sec: float = 5 * 3600 + 60

    # Fallback retention for orphan jobs (caller never fetched the result).
    # Successful fetches delete the job immediately in a background task.
    job_retention_minutes: int = 60

    stale_running_hours: int = 6

    # ---- DoS hardening ------------------------------------------------
    # Max chunks-of-upload-bytes wait before we abort a slow client.
    # Slowloris-style attack: open a connection and dribble bytes forever
    # so the worker stays blocked. 30s per chunk = ~16 KB/s minimum, which
    # is way below anything a real user has.
    upload_chunk_timeout_sec: float = 30.0
    # Hard cap on `queued` jobs. New uploads beyond this get 503. Stops a
    # caller from pushing thousands of tiny jobs and bloating SQLite.
    max_queue_depth: int = 50
    # Per-IP read rate-limit for read-only /api/jobs* endpoints.
    rate_read_per_ip: str = "120/minute"

    cpu_default_ratio: float = 0.22
    cpu_default_overhead: float = 8.0
    cuda_default_ratio: float = 0.18
    cuda_default_overhead: float = 5.0

    asr_model: str = "v3_e2e_rnnt"
    asr_load_on_startup: bool = True

    # ---- Anti-bot rate limits ------------------------------------------
    # Per-IP cap on mutating endpoints (slowapi format).
    rate_per_ip: str = Field(
        default="40/hour",
        validation_alias=AliasChoices("rate_per_ip", "rate_upload_per_ip"),
    )
    # Per-IP cap on the low-latency sync endpoint /api/dictate.
    rate_dictate_per_ip: str = "20/minute"
    # Global server-wide cap. Protects the box from coordinated spam
    # across many IPs since each transcription is real CPU time.
    global_limit: int = 100
    global_window_sec: int = 3600

    # ---- Auth ----------------------------------------------------------
    # Single Bearer token guarding every /api/* endpoint. Unset → 503.
    # Reads API_TOKEN, falls back to DICTATE_TOKEN for backward compat with
    # the old setup where only /api/dictate required a token.
    api_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("api_token", "dictate_token"),
    )

    # ---- Web cabinet ---------------------------------------------------
    # Browser uploader at GET /. HTTP Basic Auth — credentials in .env.
    # If either is empty the web cabinet returns 503.
    web_user: str | None = None
    web_pass: str | None = None
    # Tight rate-limit for the browser-facing endpoints; doesn't touch /api/*
    # which keeps its own (more generous) limits for programmatic callers.
    rate_web_per_ip: str = "20/hour"


settings = Settings()
