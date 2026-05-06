"""Retention: jobs disappear after 30 days. Tested by mutating expires_at
directly in DB and running cleanup synchronously."""
from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path


def _upload(client):
    r = client.post(
        "/api/upload",
        files={"file": ("audio.mp3", b"FAKE-AUDIO" + b"\0" * 4086, "audio/mpeg")},
    )
    return r.json()


def _wait_done(client, jid: str):
    deadline = time.time() + 5
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{jid}").json()
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.05)
    raise AssertionError(r)


def test_cleanup_removes_expired_jobs_and_files(client):
    from server.cleanup import run_cleanup_once
    from server.db import SessionLocal
    from server.models import Job
    from server.utils import utcnow

    job = _upload(client)
    _wait_done(client, job["id"])

    with SessionLocal() as db:
        j = db.get(Job, job["id"])
        text_path = Path(j.clean_path)
        assert text_path.exists()
        j.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    stats = run_cleanup_once()
    assert stats["expired_jobs"] >= 1

    after = client.get(f"/api/jobs/{job['id']}")
    assert after.status_code == 404
    assert not text_path.exists()


def test_cleanup_removes_orphan_uploads(client):
    from server.cleanup import run_cleanup_once
    from server.config import settings

    orphan = settings.uploads_dir / "deadbeef00aa"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "ghost.mp3").write_bytes(b"x")

    stats = run_cleanup_once()
    assert stats["orphan_uploads"] >= 1
    assert not orphan.exists()


def test_stale_running_marked_failed(client):
    from server.cleanup import run_stale_check_once
    from server.db import SessionLocal
    from server.models import Job
    from server.utils import utcnow
    from datetime import timedelta as _td

    j = _upload(client)
    _wait_done(client, j["id"])

    with SessionLocal() as db:
        job = db.get(Job, j["id"])
        job.status = "running"
        job.started_at = utcnow() - _td(hours=24)
        job.finished_at = None
        db.commit()

    n = run_stale_check_once()
    assert n >= 1

    final = client.get(f"/api/jobs/{j['id']}").json()
    assert final["status"] == "failed"


def test_retention_is_short(client):
    from server.config import settings
    # Server now keeps results only for a short orphan-cleanup window.
    assert settings.job_retention_minutes == 60
    assert settings.cookie_days == 7


def test_finished_job_has_minute_grain_expires_at(client):
    job = _upload(client)
    final = _wait_done(client, job["id"])
    from datetime import datetime
    from server.utils import aware
    expires = aware(datetime.fromisoformat(final["expires_at"]))
    finished = aware(datetime.fromisoformat(final["finished_at"]))
    delta = expires - finished
    assert timedelta(minutes=59) < delta < timedelta(minutes=61)
