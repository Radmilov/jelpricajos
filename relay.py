"""Worker -> relay split.

RelayHub (MODE=relay, runs on the public VPS)
    Holds the latest state pushed by the worker and serves it to visitors through the
    normal website/API. It never connects to the worker; if pushes stop it reports the
    monitor as offline. Admin actions are queued and handed to the worker in the reply
    to its next push, so the worker needs no open port.

RelayPublisher (MODE=worker, runs next to the OCR at home)
    Pushes the monitor's state OUT to the relay over HTTPS on every change and as a
    heartbeat, retries with backoff, never loses a "speech ended" event while the relay
    is unreachable, and executes the admin commands it gets back.

Only `common` and the standard library are imported here, so the relay stays tiny.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import config
from common import (
    OFFLINE, OTHER_OR_NONE, SPEAKER_STATES, VUCIC_SPEAKING, Broadcaster, empty_snapshot, iso,
    parse_iso,
)

log = logging.getLogger("relay")

PROTOCOL_VERSION = 1
HISTORY_LIMIT = 200


# ---------------------------------------------------------------------------
# Relay (public side)
# ---------------------------------------------------------------------------

class _HistoryStore:
    """Duck-types the `store.list_speeches` used by /api/v1/speeches."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    async def list_speeches(self, limit: int, since_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        rows = self.rows
        if since_ts is not None:
            rows = [r for r in rows if (parse_iso(r.get("start") or "") or 0) > since_ts]
        return rows[:limit]


class RelayHub:
    is_relay = True

    def __init__(self) -> None:
        self.events = Broadcaster()
        self.store = _HistoryStore()
        self._snap: Optional[Dict[str, Any]] = None
        self._received_at = 0.0      # relay clock, when the last push arrived
        self._clock_offset = 0.0     # relay clock - worker clock, from that push
        self._was_online = False
        self._commands: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._debug_jpeg = b""
        self._task: Optional[asyncio.Task] = None
        self.pushes = 0

    # -- lifecycle (same interface as StreamMonitor) --------------------------
    async def start(self) -> None:
        if not config.RELAY_TOKEN:
            log.warning("RELAY_TOKEN is not set: the worker cannot authenticate, nothing will update")
        self._task = asyncio.create_task(self._watchdog(), name="relay-watchdog")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _watchdog(self) -> None:
        """Tell viewers promptly when the worker goes quiet (and keep SSE heartbeats)."""
        last_beat = 0.0
        while True:
            await asyncio.sleep(2.0)
            online = self.online()
            if online != self._was_online:
                self._was_online = online
                log.info("Worker is %s", "online" if online else "OFFLINE (no pushes)")
                self.broadcast_state()
            elif time.time() - last_beat >= config.SSE_HEARTBEAT_SEC:
                last_beat = time.time()
                self.broadcast_state()

    # -- state ----------------------------------------------------------------
    def online(self) -> bool:
        return self._snap is not None and time.time() - self._received_at < config.RELAY_STALE_SEC

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        online = self.online()
        if self._snap is None:
            s = empty_snapshot(now)
        else:
            s = dict(self._snap)
            age = max(0.0, now - self._received_at)
            if s.get("speaker_state") == VUCIC_SPEAKING and online:
                s["current_session_duration_sec"] = round(
                    (s.get("current_session_duration_sec") or 0.0) + age, 1)
                if s.get("simulation_active") is False:
                    for key in ("total_vucic_time_today_sec", "total_vucic_time_all_time_sec"):
                        s[key] = round((s.get(key) or 0.0) + age, 1)
        if self._snap is not None and not online:
            # We no longer know what is on air: never keep showing "speaking".
            s.update({
                "stream_status": OFFLINE, "status_detail": "Monitor offline",
                "speaker_state": OTHER_OR_NONE, "raw_classification": OTHER_OR_NONE,
                "current_session_started_at": None, "current_session_duration_sec": 0.0,
                "on_break": False, "break_started_at": None, "simulation_active": False,
            })
        s["monitor_online"] = online
        s["monitor_last_seen"] = iso(self._received_at) if self._received_at else None
        s["server_time"] = iso(now)
        s["server_epoch"] = now
        return s

    def broadcast_state(self) -> None:
        self.events.publish("state", self.snapshot())

    @property
    def debug_jpeg(self) -> bytes:
        return self._debug_jpeg

    # -- ingest (called by app.py after the token check) ------------------------
    async def ingest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("v") != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version {payload.get('v')!r}")
        snap = payload.get("snapshot")
        if not isinstance(snap, dict) or snap.get("speaker_state") not in SPEAKER_STATES:
            raise ValueError("missing or invalid snapshot")
        now = time.time()
        worker_epoch = snap.get("server_epoch")
        if isinstance(worker_epoch, (int, float)):
            self._clock_offset = now - float(worker_epoch)
            if abs(self._clock_offset) > 120:
                log.warning("Worker clock differs from relay clock by %.0f s", self._clock_offset)
        history = payload.get("history")
        if isinstance(history, list):
            self.store.rows = [r for r in history if isinstance(r, dict)][:HISTORY_LIMIT]
        self._snap, self._received_at = snap, now
        self.pushes += 1
        for event in payload.get("events") or []:
            if isinstance(event, dict) and event.get("type") == "speech_ended":
                self.events.publish("speech_ended", event.get("data") or {})
        if not self._was_online:
            log.info("Worker is online")
        self._was_online = True
        self.broadcast_state()
        commands = list(self._commands)
        self._commands.clear()
        return {"ok": True, "commands": commands}

    def ingest_frame(self, jpeg: bytes) -> None:
        if not jpeg.startswith(b"\xff\xd8"):
            raise ValueError("not a JPEG image")
        self._debug_jpeg = jpeg

    # -- admin actions: queued for the worker ------------------------------------
    async def simulate(self, state: str, persist: bool = False) -> Dict[str, Any]:
        state = state.upper()
        if state not in SPEAKER_STATES + ("RELEASE",):
            raise ValueError(f"state must be one of {SPEAKER_STATES + ('RELEASE',)}")
        self._commands.append({"op": "simulate", "state": state, "persist": bool(persist)})
        return {**self.snapshot(), "queued": True}

    async def set_next_session(self, ts: Optional[float]) -> Dict[str, Any]:
        self._commands.append({"op": "next_session", "at": iso(ts) if ts else None})
        return {"queued": True, "next_session_at": iso(ts) if ts else None}


# ---------------------------------------------------------------------------
# Worker side: push state out to the relay
# ---------------------------------------------------------------------------

def _post(url: str, body: bytes, content_type: str, timeout: float = 10.0) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type,
        "Authorization": f"Bearer {config.RELAY_TOKEN}",
        "User-Agent": "jelpricajos-worker/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class RelayPublisher:
    def __init__(self, monitor) -> None:
        self.monitor = monitor
        self.base = config.RELAY_URL
        self._events: Deque[Dict[str, Any]] = deque(maxlen=50)   # survive relay outages
        self._history_hash: Optional[str] = None
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.pushes = 0

    def check_config(self) -> None:
        if not self.base or not config.RELAY_TOKEN:
            raise RuntimeError("MODE=worker needs RELAY_URL and RELAY_TOKEN")
        local = self.base.startswith(("http://127.0.0.1", "http://localhost"))
        if not self.base.startswith("https://") and not local:
            raise RuntimeError("RELAY_URL must use https:// (the token would travel in clear text)")

    async def run(self) -> None:
        self.check_config()
        log.info("Pushing state to relay %s", self.base)
        queue = self.monitor.events.subscribe()
        last_push = last_frame = 0.0
        fails = 0
        try:
            while True:
                dirty = False
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                    dirty = self._take(msg) or dirty
                    while not queue.empty():          # coalesce bursts into one push
                        dirty = self._take(queue.get_nowait()) or dirty
                except asyncio.TimeoutError:
                    pass
                now = time.time()
                if dirty or self._events or now - last_push >= config.RELAY_HEARTBEAT_SEC:
                    if await self.push_once():
                        last_push, fails = now, 0
                    else:
                        fails += 1
                        await asyncio.sleep(min(30.0, 2.0 ** min(fails, 5)))
                        continue
                if config.RELAY_FRAME_PUSH_SEC > 0 and now - last_frame >= config.RELAY_FRAME_PUSH_SEC:
                    last_frame = now
                    await self.push_frame()
        finally:
            self.monitor.events.unsubscribe(queue)

    def _take(self, msg: str) -> bool:
        """Parse one broadcaster message; True if it changed the state."""
        if msg.startswith("event: speech_ended"):
            data = json.loads(msg.split("data: ", 1)[1])
            self._events.append({"type": "speech_ended", "data": data})
            return True
        return msg.startswith("event: state")

    async def push_once(self) -> bool:
        payload: Dict[str, Any] = {"v": PROTOCOL_VERSION, "snapshot": self.monitor.snapshot(),
                                   "events": list(self._events)}
        history = await self.monitor.store.list_speeches(HISTORY_LIMIT)
        digest = hashlib.sha1(json.dumps(history, sort_keys=True).encode()).hexdigest()
        if digest != self._history_hash:
            payload["history"] = history
        body = json.dumps(payload, ensure_ascii=False).encode()
        try:
            status, raw = await asyncio.to_thread(_post, f"{self.base}/api/ingest", body,
                                                  "application/json")
        except Exception as exc:  # network down, DNS, TLS, timeout
            return self._failed(f"{type(exc).__name__}: {exc}")
        if status != 200:
            return self._failed(f"relay answered HTTP {status}: {raw[:200]!r}")
        sent_events = len(payload["events"])
        for _ in range(sent_events):
            self._events.popleft()
        if "history" in payload:
            self._history_hash = digest
        if self.last_error:
            log.info("Relay reachable again")
        self.last_ok, self.last_error = time.time(), None
        self.pushes += 1
        try:
            commands = json.loads(raw).get("commands") or []
        except ValueError:
            commands = []
        for cmd in commands:
            await self._run_command(cmd)
        return True

    def _failed(self, why: str) -> bool:
        if why != self.last_error:
            log.warning("Push to relay failed: %s", why)
        self.last_error = why
        return False

    async def push_frame(self) -> None:
        jpeg = self.monitor.debug_jpeg
        if not jpeg:
            return
        try:
            await asyncio.to_thread(_post, f"{self.base}/api/ingest/frame", jpeg, "image/jpeg")
        except Exception:
            pass  # best effort; the state push reports connectivity problems

    async def _run_command(self, cmd: Dict[str, Any]) -> None:
        op = cmd.get("op")
        try:
            if op == "simulate":
                await self.monitor.simulate(str(cmd.get("state")), bool(cmd.get("persist")))
            elif op == "next_session":
                at = cmd.get("at")
                await self.monitor.set_next_session(parse_iso(at) if at else None)
            else:
                log.warning("Ignoring unknown relay command %r", op)
                return
            log.info("Ran relay command %s", cmd)
        except Exception:
            log.exception("Relay command failed: %r", cmd)
