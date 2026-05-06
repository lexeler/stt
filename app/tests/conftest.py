"""Test scaffolding.

Strategy:
- Each test gets a fresh empty SQLite file in tmp_path.
- We DO NOT load the real GigaAM model (too heavy, slow, and flaky).
- We monkeypatch worker._model and pipeline.transcribe_audio with fakes.
- We bypass the FastAPI lifespan by building the app manually via `create_app()`
  and disabling startup tasks via env flags before the import.
- We also disable CSRF Origin checks (TestClient does not send Origin by default).
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _set_env_for_tests(tmp_path_factory):
    """Set test-time environment BEFORE server modules import."""
    tmpdir = tmp_path_factory.mktemp("stt_test_data", numbered=False)
    os.environ.setdefault("ASR_LOAD_ON_STARTUP", "false")
    os.environ.setdefault("CSRF_CHECK", "false")
    os.environ.setdefault("DEBUG", "true")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    # Tests do many sequential uploads — give them more headroom than prod
    os.environ.setdefault("RATE_UPLOAD_PER_IP", "1000/hour")
    os.environ.setdefault("GLOBAL_LIMIT", "1000")
    os.environ["DATA_DIR"] = str(tmpdir)
    os.environ["UPLOADS_DIR"] = str(tmpdir / "uploads")
    os.environ["TRANSCRIPTS_DIR"] = str(tmpdir / "transcripts")
    os.environ["DB_PATH"] = str(tmpdir / "app.db")
    os.environ["ETA_STATE_PATH"] = str(tmpdir / "eta.json")

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    yield


def _reset_dirs(settings) -> None:
    for d in (settings.uploads_dir, settings.transcripts_dir):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def fake_pipeline(app_factory, monkeypatch):
    """Replace transcribe_audio with a deterministic fake. Avoid real ffmpeg."""
    from server import pipeline as ASR

    def fake_get_duration(path: str) -> float:
        size = Path(path).stat().st_size
        return max(1.0, min(60.0, size / 1024.0))

    def fake_transcribe_audio(model, input_path, work_dir, on_progress=None, should_cancel=None):
        if on_progress:
            on_progress(0.5, "Распознавание (fake)")
        if should_cancel and should_cancel():
            raise ASR.CancelledByUser()
        if on_progress:
            on_progress(1.0, "Готово")
        duration = fake_get_duration(input_path)
        return ASR.TranscriptResult(
            duration=duration,
            full_text="это тестовая расшифровка",
            lines=[ASR.TranscriptLine(start=0.0, end=duration, text="это тестовая расшифровка")],
        )

    monkeypatch.setattr("server.pipeline.get_duration", fake_get_duration)
    monkeypatch.setattr("server.routers.upload.get_duration", fake_get_duration)
    monkeypatch.setattr("server.pipeline.transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr("server.worker.ASR.transcribe_audio", fake_transcribe_audio)
    return fake_get_duration


@pytest.fixture
def slow_pipeline(app_factory, monkeypatch):
    """Variant that pretends to take time so cancel-while-running can be tested."""
    from server import pipeline as ASR

    started = threading.Event()

    def fake_get_duration(path: str) -> float:
        return 30.0

    def fake_transcribe_audio(model, input_path, work_dir, on_progress=None, should_cancel=None):
        started.set()
        if on_progress:
            on_progress(0.05, "Анализ пауз")
        for _ in range(50):
            if should_cancel and should_cancel():
                raise ASR.CancelledByUser()
            time.sleep(0.05)
        return ASR.TranscriptResult(duration=30.0, full_text="готово", lines=[])

    monkeypatch.setattr("server.pipeline.get_duration", fake_get_duration)
    monkeypatch.setattr("server.routers.upload.get_duration", fake_get_duration)
    monkeypatch.setattr("server.pipeline.transcribe_audio", fake_transcribe_audio)
    monkeypatch.setattr("server.worker.ASR.transcribe_audio", fake_transcribe_audio)
    return started


@pytest.fixture
def app_factory(monkeypatch):
    """Builds a fresh FastAPI app with isolated DB for each test."""
    if "server" in sys.modules:
        for mod_name in list(sys.modules):
            if mod_name == "server" or mod_name.startswith("server."):
                del sys.modules[mod_name]

    from server.config import settings
    _reset_dirs(settings)
    if settings.db_path.exists():
        settings.db_path.unlink()
    if settings.eta_state_path.exists():
        settings.eta_state_path.unlink()

    from server import worker
    from server.db import engine
    from server.models import Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    worker.set_model("FAKE-MODEL")
    worker.start_worker_thread()

    return settings


@pytest.fixture
def client(app_factory, fake_pipeline):
    from fastapi.testclient import TestClient
    from server.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def slow_client(app_factory, slow_pipeline):
    from fastapi.testclient import TestClient
    from server.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c, slow_pipeline


def make_dummy_audio(path: Path, size: int = 4096) -> Path:
    """Create a 4 KB binary file. With fake_pipeline this is treated as audio."""
    path.write_bytes(b"FAKE-AUDIO" + b"\0" * (size - 10))
    return path
