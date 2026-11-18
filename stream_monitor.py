"""Background worker: live-stream discovery, ffmpeg frame sampling, OCR,
debounced speaker state machine, SQLite persistence and SSE broadcasting."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import aiosqlite
import cv2
import numpy as np

import config
from ocr_engine import (
    OCREngine, OTHER_OR_NONE, PARTY_MEMBER_SPEAKING, VUCIC_SPEAKING, crop_roi, is_break_screen,
    placeholder_image,
)

log = logging.getLogger("monitor")

LIVE = "LIVE"
OFFLINE = "OFFLINE"
CONNECTING = "CONNECTING"
SPEAKER_STATES = (VUCIC_SPEAKING, PARTY_MEMBER_SPEAKING, OTHER_OR_NONE)


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> Optional[float]:
    """ISO-8601 -> epoch. A time without an offset is taken as this machine's local time."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def local_midnight(now: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(now or time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Live stream discovery (yt-dlp)
# ---------------------------------------------------------------------------

@dataclass
class LiveInfo:
    is_live: bool
    reason: str = ""
    stream_url: Optional[str] = None
    http_headers: Dict[str, str] = field(default_factory=dict)
    title: Optional[str] = None
    webpage_url: Optional[str] = None
    video_id: Optional[str] = None
    release_ts: Optional[float] = None  # scheduled start, for upcoming streams


def resolve_live(channel_url: str) -> LiveInfo:
    """Blocking yt-dlp lookup; never raises."""
    import yt_dlp

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": config.YTDLP_FORMAT,
        "socket_timeout": 20,
    }
    if config.YTDLP_COOKIES_FILE:
        opts["cookiefile"] = config.YTDLP_COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).replace("ERROR: ", "")
        return LiveInfo(False, reason=msg[:300])
    except Exception as exc:  # network errors, extractor bugs, …
        return LiveInfo(False, reason=f"{type(exc).__name__}: {exc}"[:300])

    if not info or info.get("_type") == "playlist":
        return LiveInfo(False, reason="Channel is not live (no active broadcast)")
    live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
    title = info.get("title")
    if not live:
        status = info.get("live_status") or "not live"
        rts = info.get("release_timestamp") if status == "is_upcoming" else None
        return LiveInfo(False, reason=f"Latest video is {status}", title=title,
                        webpage_url=info.get("webpage_url"), video_id=info.get("id"),
                        release_ts=float(rts) if rts else None)

    url = info.get("url")
    headers = info.get("http_headers") or {}
    if not url:
        for fmt in info.get("requested_formats") or []:
            if fmt.get("vcodec") not in (None, "none"):
                url = fmt.get("url")
                headers = fmt.get("http_headers") or headers
                break
    if not url:
        return LiveInfo(False, reason="Live, but no playable stream URL was returned", title=title)
    return LiveInfo(True, stream_url=url, http_headers=headers, title=title,
                    webpage_url=info.get("webpage_url") or channel_url, video_id=info.get("id"))


def find_upcoming_session() -> Optional[float]:
    """Earliest scheduled ("upcoming") stream on the channel, as an epoch; never raises.
    The channel rarely schedules streams in advance, so this usually returns None."""
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 20}
    if config.YTDLP_COOKIES_FILE:
        base["cookiefile"] = config.YTDLP_COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL({**base, "extract_flat": "in_playlist", "playlistend": 10}) as ydl:
            listing = ydl.extract_info(config.CHANNEL_STREAMS_URL, download=False) or {}
        best = None
        for entry in listing.get("entries") or []:
            if entry.get("live_status") != "is_upcoming" or not entry.get("id"):
                continue
            ts = entry.get("release_timestamp")
            if not ts:
                with yt_dlp.YoutubeDL(base) as ydl:
                    full = ydl.extract_info(f"https://www.youtube.com/watch?v={entry['id']}",
                                            download=False, process=False) or {}
                ts = full.get("release_timestamp")
            if ts and ts > time.time() and (best is None or ts < best):
                best = float(ts)
        return best
    except Exception as exc:
        log.debug("Upcoming-stream lookup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ffmpeg frame grabber
# ---------------------------------------------------------------------------

def find_ffmpeg() -> str:
    if config.FFMPEG_PATH:
        return config.FFMPEG_PATH
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("ffmpeg not found: install it or `pip install imageio-ffmpeg`") from exc


class FrameGrabber:
    """Runs ffmpeg against the HLS URL and keeps only the newest decoded frame,
    so slow OCR never builds up latency."""

    def __init__(self, url: str, headers: Dict[str, str]):
        self.url = url
        self.headers = headers
        self.w, self.h = config.FRAME_WIDTH, config.FRAME_HEIGHT
        self.proc: Optional[subprocess.Popen] = None
        self._cond = threading.Condition()
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._ts = 0.0
        self.dead = False
        self.stderr_tail: deque = deque(maxlen=20)
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        fps = 1.0 / max(0.2, config.FRAME_INTERVAL_SEC)
        cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()
                          if k.lower() not in ("accept-encoding",))
            cmd += ["-headers", hdr]
        if ".m3u8" in self.url:
            cmd += ["-live_start_index", "-1"]
        cmd += [
            "-i", self.url, "-an", "-sn", "-dn",
            "-vf", f"fps={fps:.5f},scale={self.w}:{self.h}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, bufsize=0, creationflags=flags)
        for target in (self._read_frames, self._drain_stderr):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def _read_frames(self) -> None:
        size = self.w * self.h * 3
        stdout = self.proc.stdout
        try:
            while True:
                buf = bytearray()
                while len(buf) < size:
                    chunk = stdout.read(size - len(buf))
                    if not chunk:
                        return
                    buf.extend(chunk)
                frame = np.frombuffer(bytes(buf), np.uint8).reshape(self.h, self.w, 3)
                with self._cond:
                    self._frame, self._seq, self._ts = frame, self._seq + 1, time.time()
                    self._cond.notify_all()
        except Exception:
            log.exception("Frame reader crashed")
        finally:
            with self._cond:
                self.dead = True
                self._cond.notify_all()

    def _drain_stderr(self) -> None:
        for line in iter(self.proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.stderr_tail.append(text)
                log.debug("ffmpeg: %s", text)

    def wait_frame(self, after_seq: int, timeout: float):
        """Block until a frame newer than `after_seq` exists. Returns (frame, seq, ts) or None."""
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= after_seq and not self.dead:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if self._seq <= after_seq:
                return None
            return self._frame, self._seq, self._ts

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for t in self._threads:
            t.join(timeout=2)


# ---------------------------------------------------------------------------
# Debounced speaker state machine
# ---------------------------------------------------------------------------

@dataclass
class Transition:
    prev: str
    new: str
    since: float  # timestamp of the first frame that supported the new state


class SpeakerStateMachine:
    def __init__(self, enter=None, exit_vucic=None, exit_other=None):
        self.enter = enter or config.ENTER_SPEAKING_FRAMES
        self.exit_vucic = exit_vucic or config.EXIT_VUCIC_FRAMES
        self.exit_other = exit_other or config.EXIT_OTHER_FRAMES
        self.state = OTHER_OR_NONE
        self._pending: Optional[str] = None
        self._streak = 0
        self._since = 0.0

    def _reset(self) -> None:
        self._pending, self._streak, self._since = None, 0, 0.0

    def force(self, state: str) -> None:
        self.state = state
        self._reset()

    def update(self, raw: str, ts: float) -> Optional[Transition]:
        if self.state == VUCIC_SPEAKING:
            # Any mix of non-Vučić frames counts towards leaving; one Vučić frame resets it.
            if raw == VUCIC_SPEAKING:
                self._reset()
                return None
            if self._streak == 0:
                self._since = ts
            self._streak += 1
            if self._streak >= self.exit_vucic:
                return self._go(raw)
            return None

        if raw == self.state:
            self._reset()
            return None
        if raw != self._pending:
            self._pending, self._streak, self._since = raw, 1, ts
        else:
            self._streak += 1
        need = self.enter if raw in (VUCIC_SPEAKING, PARTY_MEMBER_SPEAKING) else self.exit_other
        if self._streak >= need:
            return self._go(raw)
        return None

    def _go(self, new: str) -> Transition:
        tr = Transition(self.state, new, self._since)
        self.state = new
        self._reset()
        return tr


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class SpeechStore:
    def __init__(self, path):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(str(self.path))
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS speeches (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   start_ts REAL NOT NULL,
                   end_ts REAL NOT NULL,
                   duration_sec REAL NOT NULL DEFAULT 0,
                   video_id TEXT,
                   stream_title TEXT,
                   active INTEGER NOT NULL DEFAULT 0
               )""")
        await self.db.execute("CREATE INDEX IF NOT EXISTS idx_speeches_start ON speeches(start_ts)")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Speeches left open by a crash keep their last checkpointed end time.
        await self.db.execute("UPDATE speeches SET active = 0 WHERE active = 1")
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    async def start(self, start_ts: float, video_id, title) -> int:
        cur = await self.db.execute(
            "INSERT INTO speeches (start_ts, end_ts, duration_sec, video_id, stream_title, active)"
            " VALUES (?, ?, 0, ?, ?, 1)", (start_ts, start_ts, video_id, title))
        await self.db.commit()
        return cur.lastrowid

    async def resume_recent(self, start_ts: float, max_gap: float) -> Optional[tuple]:
        """Reopen the latest speech if it ended less than `max_gap` seconds before `start_ts`.
        Returns (id, original_start_ts) or None."""
        async with self.db.execute(
                "SELECT id, start_ts, end_ts FROM speeches WHERE active = 0 "
                "ORDER BY end_ts DESC LIMIT 1") as c:
            row = await c.fetchone()
        if not row or start_ts - row[2] > max_gap or start_ts < row[1]:
            return None
        await self.db.execute("UPDATE speeches SET active = 1 WHERE id = ?", (row[0],))
        await self.db.commit()
        return row[0], row[1]

    async def update(self, speech_id: int, end_ts: float, start_ts: float, active: bool) -> None:
        await self.db.execute(
            "UPDATE speeches SET end_ts = ?, duration_sec = ?, active = ? WHERE id = ?",
            (end_ts, max(0.0, end_ts - start_ts), 1 if active else 0, speech_id))
        await self.db.commit()

    async def list_speeches(self, limit: int, since_ts: Optional[float] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT id, start_ts, end_ts, duration_sec, video_id, stream_title FROM speeches "
               "WHERE active = 0")
        args: list = []
        if since_ts is not None:
            sql += " AND start_ts > ?"
            args.append(since_ts)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(sql, args) as c:
            rows = await c.fetchall()
        return [{"id": r[0], "start": iso(r[1]), "end": iso(r[2]), "duration_sec": round(r[3], 1),
                 "video_id": r[4], "stream_title": r[5]} for r in rows]

    async def get_setting(self, key: str) -> Optional[str]:
        async with self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as c:
            row = await c.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: Optional[str]) -> None:
        if value is None:
            await self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            await self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                  (key, value))
        await self.db.commit()

    async def stats(self, midnight: float, limit: int) -> Dict[str, Any]:
        q = self.db.execute
        async with q("SELECT COALESCE(SUM(duration_sec), 0) FROM speeches WHERE active = 0") as c:
            all_time = (await c.fetchone())[0]
        async with q("SELECT COALESCE(SUM(duration_sec), 0), COUNT(*) FROM speeches "
                     "WHERE active = 0 AND start_ts >= ?", (midnight,)) as c:
            today, count = await c.fetchone()
        async with q("SELECT MAX(end_ts) FROM speeches WHERE active = 0") as c:
            last_end = (await c.fetchone())[0]
        async with q("SELECT id, start_ts, end_ts, duration_sec, stream_title FROM speeches "
                     "WHERE active = 0 ORDER BY start_ts DESC LIMIT ?", (limit,)) as c:
            rows = await c.fetchall()
        return {
            "all_time": float(all_time), "today": float(today), "count_today": int(count),
            "last_end": last_end,
            "recent": [{"id": r[0], "start": iso(r[1]), "end": iso(r[2]),
                        "duration_sec": round(r[3], 1), "stream_title": r[4]} for r in rows],
        }


# ---------------------------------------------------------------------------
# SSE broadcaster
# ---------------------------------------------------------------------------

class Broadcaster:
    def __init__(self):
        self._subs: Set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: Dict[str, Any]) -> None:
        msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    @property
    def count(self) -> int:
        return len(self._subs)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class CurrentSpeech:
    start_ts: float
    persist: bool
    db_id: Optional[int] = None


class StreamMonitor:
    def __init__(self):
        self.store = SpeechStore(config.DB_PATH)
        self.events = Broadcaster()
        self.ocr = OCREngine()
        self.sm = SpeakerStateMachine()

        self.stream_status = CONNECTING
        self.status_detail = "Starting up"
        self.stream_title: Optional[str] = None
        self.stream_url: Optional[str] = None
        self.video_id: Optional[str] = None
        self.last_frame_at: Optional[float] = None
        self.last_live_check: Optional[float] = None

        self.current: Optional[CurrentSpeech] = None
        self.last_vucic_seen: Optional[float] = None
        self.raw_classification = OTHER_OR_NONE
        self.last_ocr_debug: Dict[str, Any] = {}
        self.sim_hold_until = 0.0
        self.last_speech_ended: Optional[Dict[str, Any]] = None
        # break ("pauza") screen
        self.on_break = False
        self.break_since: Optional[float] = None
        self._break_streak = 0
        self._nobreak_streak = 0
        self._break_first_ts: Optional[float] = None
        # next session
        self.next_session_manual: Optional[float] = None
        self.next_session_youtube: Optional[float] = None
        self._last_upcoming_check = 0.0
        self._stats: Dict[str, Any] = {"all_time": 0.0, "today": 0.0, "count_today": 0,
                                       "last_end": None, "recent": []}
        self._debug_jpeg = self._encode(placeholder_image("Waiting for the first frame..."))
        self._tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        await self.store.open()
        await self.refresh_stats()
        await self.load_next_session()
        self._tasks = [
            asyncio.create_task(self._load_ocr(), name="ocr-load"),
            asyncio.create_task(self._main_loop(), name="stream-loop"),
            asyncio.create_task(self._ticker(), name="ticker"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.current and self.current.persist and self.current.db_id:
            end = self.last_vucic_seen or time.time()
            await self.store.update(self.current.db_id, end, self.current.start_ts, active=False)
        await self.store.close()

    async def _load_ocr(self) -> None:
        try:
            await asyncio.to_thread(self.ocr.load)
        except Exception:
            pass  # error stored on self.ocr.error and shown in the UI
        self.broadcast_state()

    # -- snapshot / broadcast ---------------------------------------------------
    @property
    def speaker_state(self) -> str:
        return self.sm.state

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        cur_dur = (now - self.current.start_ts) if (self.current and self.speaker_state == VUCIC_SPEAKING) else 0.0
        today, all_time, count = self._stats["today"], self._stats["all_time"], self._stats["count_today"]
        if self.current and self.current.persist:
            all_time += cur_dur
            if self.current.start_ts >= local_midnight(now):
                today += cur_dur
                count += 1
        return {
            "stream_status": self.stream_status,
            "status_detail": self.status_detail,
            "stream_title": self.stream_title,
            "stream_url": self.stream_url,
            "video_id": self.video_id,
            "speaker_state": self.speaker_state,
            "raw_classification": self.raw_classification,
            "current_session_started_at": iso(self.current.start_ts) if self.current else None,
            "current_session_duration_sec": round(cur_dur, 1),
            "total_vucic_time_today_sec": round(today, 1),
            "total_vucic_time_all_time_sec": round(all_time, 1),
            "speech_count_today": count,
            "last_spoke_timestamp": iso(self._stats["last_end"]),
            "recent_speeches": self._stats["recent"],
            "last_speech_ended": self.last_speech_ended,
            "on_break": self.on_break,
            "break_started_at": iso(self.break_since) if self.on_break else None,
            **self._next_session(now),
            "last_ocr_debug": self.last_ocr_debug,
            "last_frame_at": iso(self.last_frame_at),
            "last_live_check": iso(self.last_live_check),
            "ocr_ready": self.ocr.ready,
            "ocr_error": self.ocr.error,
            "ocr_backend": self.ocr.backend,
            "ocr_counts": dict(self.ocr.counts),
            "simulation_active": now < self.sim_hold_until,
            "server_time": iso(now),
            "server_epoch": now,
        }

    def broadcast_state(self) -> None:
        self.events.publish("state", self.snapshot())

    async def refresh_stats(self) -> None:
        self._stats = await self.store.stats(local_midnight(), config.RECENT_SPEECHES_LIMIT)

    @staticmethod
    def _encode(img: np.ndarray) -> bytes:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, config.DEBUG_JPEG_QUALITY])
        return buf.tobytes() if ok else b""

    @property
    def debug_jpeg(self) -> bytes:
        return self._debug_jpeg

    # -- state transitions ---------------------------------------------------
    async def _apply(self, tr: Transition, persist: bool = True) -> None:
        log.info("Speaker state %s -> %s", tr.prev, tr.new)
        if tr.prev == VUCIC_SPEAKING and self.current:
            await self._end_speech()
        if tr.new == VUCIC_SPEAKING:
            await self._start_speech(tr.since, persist)
        self.broadcast_state()

    async def _start_speech(self, start_ts: float, persist: bool) -> None:
        self.current = CurrentSpeech(start_ts=start_ts, persist=persist)
        self.last_vucic_seen = start_ts
        if persist:
            resumed = await self.store.resume_recent(start_ts, config.SPEECH_MERGE_GAP_SEC)
            if resumed:
                self.current.db_id, self.current.start_ts = resumed
                log.info("Continuing speech #%s (gap under %.0fs)", resumed[0], config.SPEECH_MERGE_GAP_SEC)
                await self.refresh_stats()
            else:
                self.current.db_id = await self.store.start(start_ts, self.video_id, self.stream_title)

    async def _end_speech(self) -> None:
        cur, self.current = self.current, None
        end = max(cur.start_ts, self.last_vucic_seen or time.time())
        duration = end - cur.start_ts
        if cur.persist and cur.db_id:
            await self.store.update(cur.db_id, end, cur.start_ts, active=False)
            await self.refresh_stats()
        log.info("Vučić finished speaking after %.0fs%s", duration,
                 "" if cur.persist else " (simulated, not stored)")
        self.last_speech_ended = {
            "duration_sec": round(duration, 1), "start": iso(cur.start_ts), "end": iso(end),
            "simulated": not cur.persist, "ended_epoch": round(time.time(), 3),
        }
        self.events.publish("speech_ended", self.last_speech_ended)

    async def _handle_classification(self, raw: str, ts: float) -> None:
        self.raw_classification = raw
        if time.time() < self.sim_hold_until:
            return
        async with self._lock:
            tr = self.sm.update(raw, ts)
            if tr:
                await self._apply(tr)
            if raw == VUCIC_SPEAKING and self.speaker_state == VUCIC_SPEAKING:
                self.last_vucic_seen = ts

    async def _force_state(self, new: str, persist: bool) -> None:
        async with self._lock:
            prev = self.speaker_state
            if prev == new:
                return
            self.sm.force(new)
            await self._apply(Transition(prev, new, time.time()), persist=persist)

    async def simulate(self, state: str, persist: bool = False) -> Dict[str, Any]:
        state = state.upper()
        if state == "RELEASE":
            self.sim_hold_until = 0.0
            self.broadcast_state()
            return self.snapshot()
        if state not in SPEAKER_STATES:
            raise ValueError(f"state must be one of {SPEAKER_STATES + ('RELEASE',)}")
        self.sim_hold_until = time.time() + config.SIMULATION_HOLD_SEC
        if state == VUCIC_SPEAKING or self.speaker_state == VUCIC_SPEAKING:
            self.last_vucic_seen = time.time()
        await self._force_state(state, persist)
        self.broadcast_state()
        return self.snapshot()

    async def _go_offline(self, reason: str) -> None:
        self._reset_break()
        changed = self.stream_status != OFFLINE or self.status_detail != reason
        self.stream_status = OFFLINE
        self.status_detail = reason
        if self.speaker_state != OTHER_OR_NONE and time.time() >= self.sim_hold_until:
            await self._force_state(OTHER_OR_NONE, persist=True)
        self.raw_classification = OTHER_OR_NONE
        if changed:
            self.broadcast_state()

    # -- break screen ----------------------------------------------------------
    def _reset_break(self) -> None:
        if self.on_break:
            log.info("Break ended (stream went offline)")
        self.on_break, self.break_since = False, None
        self._break_streak = self._nobreak_streak = 0
        self._break_first_ts = None

    async def _update_break(self, is_break: bool, ts: float) -> None:
        if is_break:
            if self._break_streak == 0:
                self._break_first_ts = ts
            self._break_streak += 1
            self._nobreak_streak = 0
            if not self.on_break and self._break_streak >= config.BREAK_ENTER_FRAMES:
                self.on_break, self.break_since = True, self._break_first_ts
                log.info("Break started")
                # Nobody is speaking during a break: close an open speech.
                if self.speaker_state != OTHER_OR_NONE and time.time() >= self.sim_hold_until:
                    await self._force_state(OTHER_OR_NONE, persist=True)
                self.raw_classification = OTHER_OR_NONE
                self.broadcast_state()
        else:
            self._nobreak_streak += 1
            self._break_streak = 0
            if self.on_break and self._nobreak_streak >= config.BREAK_EXIT_FRAMES:
                log.info("Break ended after %.0fs", ts - (self.break_since or ts))
                self.on_break, self.break_since = False, None
                self.broadcast_state()

    # -- next session ------------------------------------------------------------
    def _next_session(self, now: float) -> Dict[str, Any]:
        for ts, source in ((self.next_session_youtube, "youtube"), (self.next_session_manual, "manual")):
            if ts and ts > now:
                return {"next_session_at": iso(ts), "next_session_source": source}
        return {"next_session_at": None, "next_session_source": None}

    async def load_next_session(self) -> None:
        raw = await self.store.get_setting("next_session_at") or config.NEXT_SESSION_AT
        self.next_session_manual = parse_iso(raw) if raw else None

    async def set_next_session(self, ts: Optional[float]) -> Dict[str, Any]:
        self.next_session_manual = ts
        await self.store.set_setting("next_session_at", iso(ts) if ts else None)
        self.broadcast_state()
        return self._next_session(time.time())

    async def _check_upcoming(self, info: "LiveInfo") -> None:
        if info.release_ts and info.release_ts > time.time():
            if self.next_session_youtube != info.release_ts:
                self.next_session_youtube = info.release_ts
                self.broadcast_state()
            return
        if time.time() - self._last_upcoming_check < config.UPCOMING_CHECK_INTERVAL_SEC:
            return
        self._last_upcoming_check = time.time()
        ts = await asyncio.to_thread(find_upcoming_session)
        if ts != self.next_session_youtube:
            self.next_session_youtube = ts
            self.broadcast_state()

    # -- loops -----------------------------------------------------------------
    async def _ticker(self) -> None:
        last_stats = last_beat = last_ckpt = time.time()
        day = local_midnight()
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            try:
                if local_midnight(now) != day or now - last_stats > 60:
                    day = local_midnight(now)
                    last_stats = now
                    await self.refresh_stats()
                    self.broadcast_state()
                cur = self.current
                if (cur and cur.persist and cur.db_id
                        and now - last_ckpt >= config.CHECKPOINT_INTERVAL_SEC):
                    last_ckpt = now
                    end = max(cur.start_ts, self.last_vucic_seen or now)
                    await self.store.update(cur.db_id, end, cur.start_ts, active=True)
                # A simulation that expires while no stream is being OCR'd reverts to idle.
                if (self.sim_hold_until and now >= self.sim_hold_until
                        and self.stream_status != LIVE):
                    self.sim_hold_until = 0.0
                    if self.speaker_state != OTHER_OR_NONE:
                        self.last_vucic_seen = now
                        await self._force_state(OTHER_OR_NONE, persist=True)
                if now - last_beat >= config.SSE_HEARTBEAT_SEC:
                    last_beat = now
                    self.broadcast_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ticker error")

    async def _main_loop(self) -> None:
        while True:
            try:
                info = await asyncio.to_thread(resolve_live, config.CHANNEL_LIVE_URL)
                self.last_live_check = time.time()
                if not info.is_live:
                    log.info("Not live: %s", info.reason)
                    await self._go_offline(info.reason or "Channel is not live")
                    await self._check_upcoming(info)
                    await asyncio.sleep(config.CHANNEL_POLL_INTERVAL_SEC)
                    continue
                self.stream_title = info.title
                self.stream_url = info.webpage_url
                self.video_id = info.video_id
                if self.stream_status != LIVE:
                    self.stream_status = CONNECTING
                self.status_detail = "Connecting to HLS stream"
                self.broadcast_state()
                log.info("Live: %s (%s)", info.title, info.webpage_url)
                await self._consume_stream(info)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Monitor loop error")
                self.status_detail = f"Error: {exc}"
                self.broadcast_state()
            await asyncio.sleep(config.RECONNECT_BACKOFF_SEC)

    async def _consume_stream(self, info: LiveInfo) -> None:
        grabber = FrameGrabber(info.stream_url, info.http_headers)
        grabber.start()
        started = time.time()
        seq = 0
        try:
            while True:
                if time.time() - started > config.STREAM_URL_REFRESH_SEC:
                    log.info("Refreshing stream URL")
                    return
                got = await asyncio.to_thread(grabber.wait_frame, seq, config.FRAME_STALL_TIMEOUT_SEC)
                if got is None:
                    tail = " | ".join(list(grabber.stderr_tail)[-3:])
                    reason = "stream ended" if grabber.dead else "no frames (stalled)"
                    log.warning("ffmpeg %s; reconnecting. %s", reason, tail)
                    self.stream_status = CONNECTING
                    self.status_detail = f"Reconnecting: {reason}"
                    self.broadcast_state()
                    return
                frame, seq, ts = got
                self.last_frame_at = ts
                if self.stream_status != LIVE:
                    self.stream_status = LIVE
                    self.status_detail = "Monitoring live broadcast"
                    self.broadcast_state()
                if config.BREAK_DETECTION_ENABLED:
                    brk = await asyncio.to_thread(is_break_screen, frame)
                    await self._update_break(brk, ts)
                    if brk or self.on_break:
                        # Break screen: no caption to read, skip OCR (saves CPU).
                        self._debug_jpeg = self._encode(crop_roi(frame))
                        self.last_ocr_debug = {"note": "Break screen (pauza)", "frame_at": iso(ts)}
                        continue
                if not self.ocr.ready:
                    self._debug_jpeg = self._encode(crop_roi(frame))
                    self.last_ocr_debug = {"note": self.ocr.error or "OCR model is still loading…"}
                    continue
                analysis, annotated = await asyncio.to_thread(self.ocr.analyze_frame, frame)
                self._debug_jpeg = self._encode(annotated)
                self.last_ocr_debug = {**analysis.to_debug(), "frame_at": iso(ts)}
                prev_raw = self.raw_classification
                prev_state = self.speaker_state
                if (config.HOLD_STATE_WHEN_OCCLUDED and (analysis.occluded or analysis.uncertain)
                        and analysis.classification != self.speaker_state):
                    # Animated chyron covers the caption, or the caption structure is
                    # ambiguous: inconclusive frame, hold the current state.
                    tag = "OCCLUDED" if analysis.occluded else "UNCERTAIN"
                    self.raw_classification = f"{tag} ({analysis.classification})"
                    if prev_raw != self.raw_classification:
                        self.broadcast_state()
                    continue
                await self._handle_classification(analysis.classification, ts)
                if prev_raw != self.raw_classification and prev_state == self.speaker_state:
                    self.broadcast_state()
        finally:
            await asyncio.to_thread(grabber.stop)
