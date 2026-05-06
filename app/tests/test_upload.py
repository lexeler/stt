"""Upload + job lifecycle (queue → done → text → download)."""
from __future__ import annotations

import time

import pytest


def _wait_done(client, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if last.get("status") in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not finish in {timeout}s; last={last}")


def _upload(client, name: str = "audio.mp3", content: bytes = b"FAKE-AUDIO" + b"\0" * 4086) -> dict:
    r = client.post(
        "/api/upload",
        files={"file": (name, content, "audio/mpeg")},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_anyone_can_upload_without_registration(client):
    job = _upload(client)
    final = _wait_done(client, job["id"])
    assert final["status"] == "done"
    assert final["progress"] == 1.0

    res = client.get(f"/api/jobs/{final['id']}/result")
    assert res.status_code == 200
    body = res.json()
    assert "тестовая расшифровка" in body["clean"]
    assert "0.00" in body["timestamps"]

    # Server should auto-wipe after the result is fetched. Give the
    # background task a moment to run, then confirm the job is gone.
    import time as _time
    _time.sleep(0.2)
    after = client.get(f"/api/jobs/{final['id']}")
    assert after.status_code == 404


def test_listing_only_shows_own_browser_jobs(client):
    job = _upload(client)
    _wait_done(client, job["id"])

    own = client.get("/api/jobs").json()
    assert own["total"] == 1
    assert own["jobs"][0]["id"] == job["id"]

    client.cookies.clear()
    other = client.get("/api/jobs").json()
    assert other["total"] == 0


def test_unknown_browser_cannot_read_others_text(client):
    job = _upload(client)
    _wait_done(client, job["id"])

    client.cookies.clear()
    r = client.get(f"/api/jobs/{job['id']}/result")
    assert r.status_code == 404


def test_upload_rejects_non_audio(client):
    r = client.post(
        "/api/upload",
        files={"file": ("hack.exe", b"\x90" * 5000, "application/x-msdownload")},
    )
    assert r.status_code == 415


def test_upload_rejects_too_short_file(client):
    r = client.post(
        "/api/upload",
        files={"file": ("tiny.mp3", b"a", "audio/mpeg")},
    )
    assert r.status_code == 400


def test_result_endpoint_returns_filename_unicode(client):
    job = _upload(client, name="лекция №3.mp3")
    final = _wait_done(client, job["id"])

    r = client.get(f"/api/jobs/{final['id']}/result")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "лекция №3.mp3"
    assert body["clean"]
    assert body["timestamps"]


def test_no_quota_lots_of_uploads_all_succeed(client):
    # We removed all quotas: ten back-to-back uploads should all be 200.
    for i in range(10):
        r = client.post(
            "/api/upload",
            files={"file": (f"f{i}.mp3", b"FAKE-AUDIO" + b"\0" * 4086, "audio/mpeg")},
        )
        assert r.status_code == 200, f"upload #{i}: {r.status_code} {r.text}"


def test_clear_all_history_endpoint(client):
    for i in range(3):
        job = _upload(client, name=f"f{i}.mp3")
        _wait_done(client, job["id"])

    r = client.delete("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["removed"] == 3
    assert body["skipped_active"] == 0

    after = client.get("/api/jobs").json()
    assert after["total"] == 0
