"""Worker -> relay split: relay HTTP API and the worker-side publisher.
Run: python -m pytest tests/test_relay.py -q   (needs: pip install httpx)"""
import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import common  # noqa: E402

TOKEN = "test-worker-token"
ADMIN = "test-admin-token"


@pytest.fixture()
def relay_app(monkeypatch):
    monkeypatch.setenv("MODE", "relay")
    monkeypatch.setenv("RELAY_TOKEN", TOKEN)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "0")
    import config
    importlib.reload(config)
    sys.modules.pop("relay", None)
    sys.modules.pop("app", None)
    import app as relay_app_module
    with TestClient(relay_app_module.app) as client:
        yield client, relay_app_module
    # restore the default configuration for the other test modules
    for key in ("MODE", "RELAY_TOKEN", "ADMIN_TOKEN", "RATE_LIMIT_PER_MIN"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(config)
    sys.modules.pop("app", None)
    sys.modules.pop("relay", None)


def worker_snapshot(**over):
    s = common.empty_snapshot()
    s.update({"stream_status": "LIVE", "status_detail": r"Error: C:\Users\someone\cookies.txt",
              "ocr_ready": True, "video_id": "abc"})
    s.update(over)
    return s


def push(client, snapshot, token=TOKEN, **extra):
    body = {"v": 1, "snapshot": snapshot, "events": [], **extra}
    return client.post("/api/ingest", json=body, headers={"Authorization": f"Bearer {token}"})


def test_ingest_requires_the_worker_token(relay_app):
    client, _ = relay_app
    assert client.post("/api/ingest", json={"v": 1}).status_code == 401
    assert push(client, worker_snapshot(), token="wrong").status_code == 401
    assert push(client, {"speaker_state": "HACKED"}).status_code == 400


def test_push_updates_public_api(relay_app):
    client, _ = relay_app
    assert client.get("/api/v1/status").json()["monitor_online"] is False   # nothing pushed yet
    started = common.iso(time.time() - 30)
    history = [{"id": 1, "start": started, "end": None, "duration_sec": 30.0}]
    r = push(client, worker_snapshot(speaker_state="VUCIC_SPEAKING", current_session_started_at=started,
                                     current_session_duration_sec=30.0), history=history)
    assert r.status_code == 200 and r.json()["commands"] == []
    st = client.get("/api/v1/status").json()
    assert st["vucic_speaking"] and st["monitor_online"] and st["stream_status"] == "LIVE"
    assert client.get("/api/v1/speeches").json()["count"] == 1
    # the public never sees internal error details pushed by the worker
    assert "someone" not in client.get("/api/state").text
    assert client.get("/api/debug/frame.jpg").status_code == 204


def test_silent_worker_is_reported_offline_not_speaking(relay_app):
    client, mod = relay_app
    push(client, worker_snapshot(speaker_state="VUCIC_SPEAKING", current_session_started_at=common.iso(time.time())))
    mod.monitor._received_at -= 3600          # last push an hour ago
    st = client.get("/api/v1/status").json()
    assert st["monitor_online"] is False and not st["vucic_speaking"]
    assert st["speaker_state"] == "OTHER_OR_NONE" and st["stream_status"] == "OFFLINE"


def test_speech_ended_event_reaches_viewers(relay_app):
    client, mod = relay_app
    q = mod.monitor.events.subscribe()
    ev = {"type": "speech_ended", "data": {"duration_sec": 42.0, "start": "a", "end": "b", "simulated": False}}
    push(client, worker_snapshot(), events=[ev])
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    assert any(m.startswith("event: speech_ended") and '"duration_sec": 42.0' in m for m in msgs)


def test_admin_actions_are_queued_for_the_worker(relay_app):
    client, _ = relay_app
    # through the internet (no admin token): refused
    assert client.post("/api/simulate", json={"state": "VUCIC_SPEAKING"}).status_code == 403
    h = {"X-Admin-Token": ADMIN}
    assert client.post("/api/simulate", json={"state": "VUCIC_SPEAKING"}, headers=h).json()["queued"]
    assert client.post("/api/admin/next-session", json={"at": "2026-11-20T10:00"}, headers=h).json()["queued"]
    cmds = push(client, worker_snapshot()).json()["commands"]
    assert [c["op"] for c in cmds] == ["simulate", "next_session"]
    assert push(client, worker_snapshot()).json()["commands"] == []     # delivered once


def test_frame_upload_checks_token_and_format(relay_app):
    client, _ = relay_app
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    assert client.post("/api/ingest/frame", content=jpeg).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/ingest/frame", content=b"not an image", headers=auth).status_code == 400
    assert client.post("/api/ingest/frame", content=jpeg, headers=auth).status_code == 200
    assert client.get("/api/debug/frame.jpg").content == jpeg


# --- worker side ------------------------------------------------------------------

class FakeStore:
    async def list_speeches(self, limit, since_ts=None):
        return [{"id": 7, "start": "x", "end": "y", "duration_sec": 5.0}]


class FakeMonitor:
    def __init__(self):
        self.events = common.Broadcaster()
        self.store = FakeStore()
        self.debug_jpeg = b""
        self.ran = []

    def snapshot(self):
        return worker_snapshot()

    async def simulate(self, state, persist=False):
        self.ran.append(("simulate", state))

    async def set_next_session(self, ts):
        self.ran.append(("next_session", ts))


def test_publisher_keeps_events_until_relay_accepts_them(monkeypatch):
    import relay
    sent = []
    answers = [(503, b"down"), (200, json.dumps({"ok": True, "commands": [
        {"op": "next_session", "at": "2026-11-20T10:00:00+01:00"}]}).encode())]

    def fake_post(url, body, content_type, timeout=10.0):
        sent.append(json.loads(body))
        return answers.pop(0)

    monkeypatch.setattr(relay, "_post", fake_post)
    mon = FakeMonitor()
    pub = relay.RelayPublisher(mon)
    pub._take('event: speech_ended\ndata: {"duration_sec": 9.0}\n\n')

    assert asyncio.run(pub.push_once()) is False            # relay down: event kept
    assert len(pub._events) == 1 and pub.last_error
    assert asyncio.run(pub.push_once()) is True             # relay back: event delivered
    assert len(pub._events) == 0 and pub.last_error is None
    assert sent[1]["events"][0]["data"]["duration_sec"] == 9.0
    assert "history" in sent[1]                              # history sent once…
    assert mon.ran and mon.ran[0][0] == "next_session"      # …and the queued command ran


def test_publisher_refuses_plain_http_to_remote_relay(monkeypatch):
    import config
    import relay
    monkeypatch.setattr(config, "RELAY_TOKEN", "t")
    monkeypatch.setattr(config, "RELAY_URL", "http://example.org")
    with pytest.raises(RuntimeError, match="https"):
        relay.RelayPublisher(FakeMonitor()).check_config()
    monkeypatch.setattr(config, "RELAY_URL", "http://127.0.0.1:8100")
    relay.RelayPublisher(FakeMonitor()).check_config()     # local relay is fine
