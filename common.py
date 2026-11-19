"""Small, dependency-free pieces shared by every mode (standalone, worker, relay).

The relay imports only this module (plus FastAPI), so it runs on a tiny VPS without
OpenCV, PyTorch, EasyOCR, yt-dlp or ffmpeg.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

VUCIC_SPEAKING = "VUCIC_SPEAKING"
PARTY_MEMBER_SPEAKING = "PARTY_MEMBER_SPEAKING"
OTHER_OR_NONE = "OTHER_OR_NONE"
SPEAKER_STATES = (VUCIC_SPEAKING, PARTY_MEMBER_SPEAKING, OTHER_OR_NONE)

LIVE = "LIVE"
OFFLINE = "OFFLINE"
CONNECTING = "CONNECTING"


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


class Broadcaster:
    """Fan-out of pre-serialised SSE messages to every subscriber queue."""

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


def empty_snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    """A complete, neutral state (used by the relay before the worker has reported)."""
    now = now or time.time()
    return {
        "stream_status": OFFLINE, "status_detail": "Waiting for the monitor",
        "stream_title": None, "stream_url": None, "video_id": None,
        "speaker_state": OTHER_OR_NONE, "raw_classification": OTHER_OR_NONE,
        "current_session_started_at": None, "current_session_duration_sec": 0.0,
        "total_vucic_time_today_sec": 0.0, "total_vucic_time_all_time_sec": 0.0,
        "speech_count_today": 0, "last_spoke_timestamp": None, "recent_speeches": [],
        "last_speech_ended": None, "on_break": False, "break_started_at": None,
        "next_session_at": None, "next_session_source": None,
        "last_ocr_debug": {}, "last_frame_at": None, "last_live_check": None,
        "ocr_ready": False, "ocr_error": None, "ocr_backend": "remote", "ocr_counts": {},
        "simulation_active": False, "server_time": iso(now), "server_epoch": now,
    }
