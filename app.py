"""FastAPI server for the Parlament Srbija / Vučić speaker monitor.

Run:  python app.py      (or)   uvicorn app:app --host 127.0.0.1 --port 8000

Public, read-only API lives under /api/v1 (documented at /docs). The dashboard uses
/api/state and /api/events. /api/simulate is admin-only.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, Deque, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from ocr_engine import PARTY_MEMBER_SPEAKING, VUCIC_SPEAKING
from stream_monitor import StreamMonitor

logging.basicConfig(level=config.LOG_LEVEL,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC_DIR = config.BASE_DIR / "static"
API_VERSION = "1.0"
monitor = StreamMonitor()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await monitor.start()
    log.info("Dashboard: http://%s:%s  ·  API docs: /docs", config.HOST, config.PORT)
    try:
        yield
    finally:
        await monitor.stop()


app = FastAPI(
    title="Vučić Speaking Monitor API",
    version=API_VERSION,
    description=(
        "Real-time detection of whether Aleksandar Vučić is speaking in the National Assembly "
        "of Serbia, based on OCR of the @ParlamentSrbija YouTube live stream.\n\n"
        "* `GET /api/v1/status` — compact current status (poll every 2–5 s)\n"
        "* `GET /api/v1/events` — Server-Sent Events push stream (preferred over polling)\n"
        "* `GET /api/v1/stats`, `GET /api/v1/speeches` — statistics and history\n\n"
        f"Rate limit: {config.RATE_LIMIT_PER_MIN} requests/minute per client."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    max_age=86400,
)
app.add_middleware(GZipMiddleware, minimum_size=config.GZIP_MIN_SIZE, compresslevel=6)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Client identity, admin check, rate limiting
# ---------------------------------------------------------------------------

_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")


def _is_loopback(host: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return host == "localhost"


def client_ip(request: Request) -> str:
    """Socket IP, or the forwarded client IP when the request came through a local proxy
    (e.g. cloudflared / nginx on this machine). Forwarded headers from remote peers are
    ignored, so they can't be spoofed to dodge the rate limit."""
    peer = request.client.host if request.client else "unknown"
    if _is_loopback(peer):
        h = request.headers
        fwd = h.get("cf-connecting-ip") or h.get("x-real-ip") or \
            (h.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return peer


def is_admin(request: Request) -> bool:
    token = request.headers.get("x-admin-token", "")
    if config.ADMIN_TOKEN and token and hmac.compare_digest(token, config.ADMIN_TOKEN):
        return True
    peer = request.client.host if request.client else None
    # A request relayed by a local tunnel/proxy also arrives from 127.0.0.1, so it only
    # counts as local if it carries no forwarding headers.
    proxied = any(k in request.headers for k in _PROXY_HEADERS)
    return _is_loopback(peer) and not proxied


_hits: Dict[str, Deque[float]] = defaultdict(deque)
_sse_clients: Dict[str, int] = defaultdict(int)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    if config.RATE_LIMIT_PER_MIN <= 0 or not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    ip = client_ip(request)
    if not is_admin(request):
        now = time.monotonic()
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_PER_MIN:
            retry = max(1, int(60 - (now - q[0])) + 1)
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(retry),
                                         "X-RateLimit-Limit": str(config.RATE_LIMIT_PER_MIN),
                                         "X-RateLimit-Remaining": "0"})
        q.append(now)
        if len(_hits) > 50_000:  # memory guard
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)
        remaining = config.RATE_LIMIT_PER_MIN - len(q)
    else:
        remaining = config.RATE_LIMIT_PER_MIN
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(config.RATE_LIMIT_PER_MIN)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


# ---------------------------------------------------------------------------
# Public payloads
# ---------------------------------------------------------------------------

def public_status(s: Dict[str, Any]) -> Dict[str, Any]:
    state = s["speaker_state"]
    return {
        "api_version": API_VERSION,
        "vucic_speaking": state == VUCIC_SPEAKING,
        "party_member_speaking": state == PARTY_MEMBER_SPEAKING,
        "speaker_state": state,
        "stream_status": s["stream_status"],
        "stream_title": s["stream_title"],
        "stream_url": s["stream_url"],
        "video_id": s["video_id"],
        "current_session_started_at": s["current_session_started_at"],
        "current_session_duration_sec": s["current_session_duration_sec"],
        "last_spoke_timestamp": s["last_spoke_timestamp"],
        "simulated": s["simulation_active"],
        "last_frame_at": s["last_frame_at"],
        "last_speech_ended": s["last_speech_ended"],
        "on_break": s["on_break"],
        "break_started_at": s["break_started_at"],
        "next_session_at": s["next_session_at"],
        "updated_at": s["server_time"],
        "server_epoch": round(s["server_epoch"], 3),
    }


def public_stats(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "total_vucic_time_today_sec": s["total_vucic_time_today_sec"],
        "total_vucic_time_all_time_sec": s["total_vucic_time_all_time_sec"],
        "speech_count_today": s["speech_count_today"],
        "last_spoke_timestamp": s["last_spoke_timestamp"],
        "updated_at": s["server_time"],
    }


# Status messages the public may see verbatim. Anything else (yt-dlp/ffmpeg/OCR error text,
# which can contain file paths, user names or URLs) is replaced by a generic message.
_SAFE_DETAIL_PREFIXES = (
    "Starting up", "Monitoring live broadcast", "Connecting to HLS stream",
    "Reconnecting: stream ended", "Reconnecting: no frames", "Channel is not live",
    "Latest video is",
)


def _safe_detail(status: str, detail: Optional[str]) -> str:
    detail = detail or ""
    if detail.startswith(_SAFE_DETAIL_PREFIXES):
        return detail
    if status == "OFFLINE":
        return "Channel is not live"
    if status == "CONNECTING":
        return "Connecting to the stream"
    return "Monitoring live broadcast" if status == "LIVE" else ""


def redact_snapshot(s: Dict[str, Any]) -> Dict[str, Any]:
    """Dashboard state for non-admin viewers: same data, minus internal error details."""
    out = dict(s)
    out["status_detail"] = _safe_detail(s["stream_status"], s.get("status_detail"))
    out["ocr_error"] = "OCR unavailable" if s.get("ocr_error") else None
    dbg = s.get("last_ocr_debug") or {}
    if "note" in dbg:
        dbg = {**dbg, "note": "OCR unavailable" if s.get("ocr_error") else "OCR model is loading"}
    out["last_ocr_debug"] = dbg
    return out


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _event_stream(request: Request, compact: bool):
    """Shared SSE generator. `compact` = public v1 payloads (deduplicated)."""
    ip = client_ip(request)
    if not is_admin(request):
        total = sum(_sse_clients.values())
        if _sse_clients[ip] >= config.SSE_MAX_PER_CLIENT or total >= config.SSE_MAX_TOTAL:
            raise HTTPException(status_code=429, detail="Too many open event streams",
                                headers={"Retry-After": "30"})
    admin = is_admin(request)
    _sse_clients[ip] += 1
    queue = monitor.events.subscribe()

    def render_state() -> str:
        snap = monitor.snapshot()
        if compact:
            return _sse("status", public_status(snap))
        return _sse("state", snap if admin else redact_snapshot(snap))

    async def gen():
        last_key = None
        try:
            yield "retry: 3000\n\n"
            first = render_state()
            yield first
            if compact:
                last_key = _status_key()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=config.SSE_HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if not compact:
                    # Broadcast messages carry the raw snapshot; re-render it redacted for the public.
                    yield msg if admin or not msg.startswith("event: state") else render_state()
                elif msg.startswith("event: speech_ended"):
                    yield msg
                elif msg.startswith("event: state"):
                    key = _status_key()
                    if key != last_key:
                        last_key = key
                        yield render_state()
        finally:
            monitor.events.unsubscribe(queue)
            _sse_clients[ip] -= 1
            if _sse_clients[ip] <= 0:
                _sse_clients.pop(ip, None)

    # Exact "text/event-stream" (no "; charset" suffix): Cloudflare tunnels only stream
    # unbuffered when the content type matches exactly.
    return StreamingResponse(gen(), headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no",
    })


def _status_key():
    s = monitor.snapshot()
    return (s["speaker_state"], s["stream_status"], s["video_id"], s["current_session_started_at"],
            s["simulation_active"], s["last_spoke_timestamp"],
            (s["last_speech_ended"] or {}).get("ended_epoch"), s["on_break"], s["next_session_at"])


# ---------------------------------------------------------------------------
# Dashboard + internal endpoints
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/state", tags=["dashboard"], summary="Full internal state (used by the dashboard)")
async def get_state(request: Request):
    snap = monitor.snapshot()
    return JSONResponse(snap if is_admin(request) else redact_snapshot(snap))


@app.get("/api/events", tags=["dashboard"], summary="Full-state SSE stream (used by the dashboard)")
async def events(request: Request):
    return await _event_stream(request, compact=False)


@app.get("/api/whoami", tags=["dashboard"], summary="Whether this client may use admin endpoints")
async def whoami(request: Request):
    return {"admin": is_admin(request)}


@app.get("/api/debug/frame.jpg", tags=["dashboard"], summary="Latest annotated OCR region")
async def debug_frame():
    return Response(content=monitor.debug_jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


class SimulateRequest(BaseModel):
    state: str
    persist: Optional[bool] = False


@app.post("/api/simulate", tags=["admin"], summary="Force a speaker state (admin only)")
async def simulate(req: SimulateRequest, request: Request):
    """{"state": "VUCIC_SPEAKING" | "PARTY_MEMBER_SPEAKING" | "OTHER_OR_NONE" | "RELEASE"}.
    Allowed from this machine, or remotely with header `X-Admin-Token`."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        return JSONResponse(await monitor.simulate(req.state, bool(req.persist)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class NextSessionRequest(BaseModel):
    at: Optional[str] = None  # ISO-8601; null/empty clears it


@app.post("/api/admin/next-session", tags=["admin"], summary="Set the next session time (admin only)")
async def set_next_session(req: NextSessionRequest, request: Request):
    """{"at": "2026-11-19T10:00"} — a time without an offset is the server's local time.
    {"at": null} clears it. A scheduled YouTube stream, if any, takes precedence."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Admin only")
    from stream_monitor import parse_iso
    ts = None
    if req.at:
        ts = parse_iso(req.at)
        if ts is None:
            raise HTTPException(status_code=400, detail="`at` must be ISO-8601, e.g. 2026-11-19T10:00")
    return JSONResponse(await monitor.set_next_session(ts))


# ---------------------------------------------------------------------------
# Public v1 API
# ---------------------------------------------------------------------------

@app.get("/api/v1/status", tags=["public v1"], summary="Is Vučić speaking right now?")
async def v1_status():
    return JSONResponse(public_status(monitor.snapshot()), headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/stats", tags=["public v1"], summary="Speaking-time statistics")
async def v1_stats():
    return JSONResponse(public_stats(monitor.snapshot()), headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/speeches", tags=["public v1"], summary="Recorded speeches, newest first")
async def v1_speeches(limit: int = Query(20, ge=1, le=200),
                      since: Optional[str] = Query(None, description="ISO-8601; only speeches starting after this")):
    since_ts = None
    if since:
        try:
            from datetime import datetime
            since_ts = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="`since` must be ISO-8601") from exc
    rows = await monitor.store.list_speeches(limit, since_ts)
    return JSONResponse({"speeches": rows, "count": len(rows)})


@app.get("/api/v1/events", tags=["public v1"], summary="Push stream (Server-Sent Events)")
async def v1_events(request: Request):
    """Events: `status` (same payload as /api/v1/status, sent on every change) and
    `speech_ended` (`{duration_sec, start, end, simulated}`). Comment keep-alives every 15 s."""
    return await _event_stream(request, compact=True)


@app.get("/api/v1/health", tags=["public v1"], summary="Liveness check")
async def v1_health():
    s = monitor.snapshot()
    return {"ok": True, "ocr_ready": s["ocr_ready"], "stream_status": s["stream_status"],
            "last_frame_at": s["last_frame_at"]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level=config.LOG_LEVEL.lower(),
                proxy_headers=False)
