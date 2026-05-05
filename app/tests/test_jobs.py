"""Cancellation, deletion, recovery."""
from __future__ import annotations

import time

import pytest


def _upload_short(client) -> dict:
    r = client.post(
        "/api/upload",
        files={"file": ("audio.mp3", b"FAKE-AUDIO" + b"\0" * 4086, "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _wait_done(client, job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}").json()
        if r.get("status") in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.05)
    raise AssertionError(f"timed out: {r}")


def test_cancel_running_job(slow_client):
    client, started_event = slow_client

    r = client.post(
        "/api/upload",
        files={"file": ("audio.mp3", b"FAKE-AUDIO" + b"\0" * 4086, "audio/mpeg")},
    )
    job = r.json()

    assert started_event.wait(timeout=2.0), "worker did not pick up the job"

    cancel = client.post(f"/api/jobs/{job['id']}/cancel")
    assert cancel.status_code == 200

    final = _wait_done(client, job["id"], timeout=5.0)
    assert final["status"] == "cancelled"


def test_cannot_delete_active_job(slow_client):
    client, started_event = slow_client
    r = client.post(
        "/api/upload",
        files={"file": ("audio.mp3", b"FAKE-AUDIO" + b"\0" * 4086, "audio/mpeg")},
    )
    job = r.json()

    started_event.wait(timeout=2.0)

    deleted = client.delete(f"/api/jobs/{job['id']}")
    assert deleted.status_code == 400

    client.post(f"/api/jobs/{job['id']}/cancel")
    _wait_done(client, job["id"], timeout=5.0)

    deleted = client.delete(f"/api/jobs/{job['id']}")
    assert deleted.status_code == 200


def test_cancelled_jobs_can_be_deleted(client):
    job = _upload_short(client)
    _wait_done(client, job["id"])

    r = client.delete(f"/api/jobs/{job['id']}")
    assert r.status_code == 200

    after = client.get("/api/jobs").json()
    assert all(j["id"] != job["id"] for j in after["jobs"])


def test_healthz_exposes_queue_state(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "model_loaded" in body
    assert "queue_depth" in body
