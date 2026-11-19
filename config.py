"""Tunable settings for the Parlament Srbija / Vučić speaker monitor.

Every value can be overridden with an environment variable of the same name
(e.g. `set OCR_GPU=1` on Windows or `export OCR_GPU=1` on macOS/Linux), or with a
`KEY=value` line in `local.env` next to this file (git-ignored; real environment
variables win over it).
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_local_env(path: Path) -> None:
    """Minimal .env reader: KEY=value lines, '#' comments, optional quotes."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env(Path(__file__).resolve().parent / "local.env")


def _env(name: str, default, cast=None):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if cast is list:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return (cast or type(default))(raw)


BASE_DIR = Path(__file__).resolve().parent

# --- Web server -------------------------------------------------------------
HOST = _env("HOST", "127.0.0.1")
PORT = _env("PORT", 8000, int)

# --- Channel / stream discovery ---------------------------------------------
CHANNEL_LIVE_URL = _env("CHANNEL_LIVE_URL", "https://www.youtube.com/@ParlamentSrbija/live")
CHANNEL_POLL_INTERVAL_SEC = _env("CHANNEL_POLL_INTERVAL_SEC", 30.0, float)
# yt-dlp format selector. Live streams expose video-only HLS variants, so prefer
# `bestvideo`. 480p (~1.3 Mbit/s) still reads the caption reliably (verified on the live stream).
YTDLP_FORMAT = _env(
    "YTDLP_FORMAT",
    "bestvideo[height<=480][protocol^=m3u8]/bestvideo[height<=480]/best[height<=480]/bestvideo/best",
)
# Optional Netscape-format cookies file if YouTube starts asking you to "confirm you're not a bot".
YTDLP_COOKIES_FILE = _env("YTDLP_COOKIES_FILE", "")
# Re-resolve the HLS URL proactively before YouTube's signed manifest URL expires (~6h).
STREAM_URL_REFRESH_SEC = _env("STREAM_URL_REFRESH_SEC", 3 * 3600.0, float)

# --- Frame sampling (ffmpeg) --------------------------------------------------
# Leave empty to use `ffmpeg` from PATH, falling back to the imageio-ffmpeg bundled binary.
FFMPEG_PATH = _env("FFMPEG_PATH", "")
FRAME_INTERVAL_SEC = _env("FRAME_INTERVAL_SEC", 2.0, float)
# Frames are scaled to this fixed size so all ratios below are stable.
FRAME_WIDTH = _env("FRAME_WIDTH", 854, int)
FRAME_HEIGHT = _env("FRAME_HEIGHT", 480, int)
# If no frame arrives for this long, ffmpeg is restarted (and the URL re-resolved).
FRAME_STALL_TIMEOUT_SEC = _env("FRAME_STALL_TIMEOUT_SEC", 25.0, float)
RECONNECT_BACKOFF_SEC = _env("RECONNECT_BACKOFF_SEC", 5.0, float)

# --- Region of interest (fractions of the full frame) ------------------------
# Calibrated on the real broadcast (Nov 2026). The caption has appeared in two places: left edge
# at x ~0.28 W (18 Nov) and hugging the bottom-left corner at x ~0.03 W (19 Nov), so the ROI
# starts at the left edge; the name line sits at ~0.87-0.94 H and the parliamentary-group line at
# ~0.93-0.99 H (the longest one, "Александар Вучић - Србија побеђује", ends at ~0.73 W).
# The top edge (0.80 H) also covers the large animated chyron's name line, which the
# occlusion check needs. A tight ROI is the biggest CPU saving; widen it if the layout changes.
ROI_Y_START = _env("ROI_Y_START", 0.80, float)
ROI_Y_END = _env("ROI_Y_END", 1.0, float)
ROI_X_START = _env("ROI_X_START", 0.0, float)
ROI_X_END = _env("ROI_X_END", 0.82, float)

# --- Preprocessing ------------------------------------------------------------
# Upscale factor applied to the ROI before OCR (helps EasyOCR with small glyphs).
OCR_UPSCALE = _env("OCR_UPSCALE", 2.0, float)
CLAHE_CLIP_LIMIT = _env("CLAHE_CLIP_LIMIT", 2.5, float)
CLAHE_TILE_GRID = _env("CLAHE_TILE_GRID", 8, int)
# "clahe" = grayscale + CLAHE contrast (recommended for EasyOCR);
# "threshold" = additionally isolate bright (white) text with a binary threshold.
PREPROCESS_MODE = _env("PREPROCESS_MODE", "clahe")
WHITE_TEXT_THRESHOLD = _env("WHITE_TEXT_THRESHOLD", 185, int)

# --- OCR engine -----------------------------------------------------------------
OCR_BACKEND = _env("OCR_BACKEND", "easyocr")  # "easyocr" or "tesseract"
OCR_LANGUAGES = _env("OCR_LANGUAGES", ["rs_cyrillic"], list)
OCR_GPU = _env("OCR_GPU", False, bool)
# CPU threads PyTorch may use for OCR (0 = library default, i.e. all cores).
OCR_THREADS = _env("OCR_THREADS", 2, int)
# Cheap pre-checks that skip EasyOCR when it can't change the answer:
#  * no bright (white) text pixels in the ROI  -> no caption -> OTHER_OR_NONE
#  * bright-pixel mask ~identical to the last OCR'd frame -> reuse that result
OCR_SKIP_ENABLED = _env("OCR_SKIP_ENABLED", True, bool)
TEXT_BRIGHT_LEVEL = _env("TEXT_BRIGHT_LEVEL", 200, int)
TEXT_MIN_BRIGHT_FRACTION = _env("TEXT_MIN_BRIGHT_FRACTION", 0.004, float)
CAPTION_REUSE_IOU = _env("CAPTION_REUSE_IOU", 0.85, float)
FORCE_OCR_EVERY_SEC = _env("FORCE_OCR_EVERY_SEC", 20.0, float)
OCR_MIN_CONFIDENCE = _env("OCR_MIN_CONFIDENCE", 0.15, float)
TESSERACT_LANG = _env("TESSERACT_LANG", "srp")
TESSERACT_CMD = _env("TESSERACT_CMD", "")

# --- Bounding-box height filter (fraction of the FULL frame height H) --------
# Measured on the live stream: caption lines give EasyOCR boxes of 5.6-6.2 % of H;
# the large animated "embossed" chyron gives 7.2-8.5 % and is deliberately rejected.
MIN_BOX_HEIGHT_RATIO = _env("MIN_BOX_HEIGHT_RATIO", 0.030, float)
MAX_BOX_HEIGHT_RATIO = _env("MAX_BOX_HEIGHT_RATIO", 0.068, float)
# When oversized overlay text (the animated chyron) covers the caption, the frame is
# "occluded" and is not allowed to change the speaker state.
HOLD_STATE_WHEN_OCCLUDED = _env("HOLD_STATE_WHEN_OCCLUDED", True, bool)
OCCLUSION_MIN_HEIGHT_RATIO = _env("OCCLUSION_MIN_HEIGHT_RATIO", 0.068, float)

# --- Name matching ------------------------------------------------------------
TARGET_NAME = _env("TARGET_NAME", "александар вучић")
FUZZY_THRESHOLD = _env("FUZZY_THRESHOLD", 0.80, float)

# --- Debounce (consecutive frames) -----------------------------------------
ENTER_SPEAKING_FRAMES = _env("ENTER_SPEAKING_FRAMES", 2, int)
EXIT_VUCIC_FRAMES = _env("EXIT_VUCIC_FRAMES", 3, int)
# Frames needed to go from PARTY_MEMBER_SPEAKING back to OTHER_OR_NONE.
EXIT_OTHER_FRAMES = _env("EXIT_OTHER_FRAMES", 2, int)

# --- Persistence / misc ------------------------------------------------------
DB_PATH = Path(_env("DB_PATH", str(BASE_DIR / "data" / "speeches.db")))
# How often an in-progress speech is checkpointed to SQLite (crash safety).
CHECKPOINT_INTERVAL_SEC = _env("CHECKPOINT_INTERVAL_SEC", 10.0, float)
# A new Vučić speech that starts within this many seconds of the previous one ending is
# treated as the same speech (long camera cuts, stream reconnects, server restarts).
SPEECH_MERGE_GAP_SEC = _env("SPEECH_MERGE_GAP_SEC", 60.0, float)
RECENT_SPEECHES_LIMIT = _env("RECENT_SPEECHES_LIMIT", 10, int)
SSE_HEARTBEAT_SEC = _env("SSE_HEARTBEAT_SEC", 15.0, float)
# While a simulated state is active, real OCR results don't change the state.
SIMULATION_HOLD_SEC = _env("SIMULATION_HOLD_SEC", 120.0, float)
DEBUG_JPEG_QUALITY = _env("DEBUG_JPEG_QUALITY", 85, int)
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# --- Public API -------------------------------------------------------------
# Set HOST=0.0.0.0 to accept connections from other machines (LAN / tunnel / reverse proxy).
# Origins allowed to call the API from browsers ("*" = anyone; read-only endpoints only).
CORS_ALLOW_ORIGINS = _env("CORS_ALLOW_ORIGINS", ["*"], list)
# Per-IP request budget for /api/* (requests per minute). 0 disables rate limiting.
# Generous on purpose: mobile carriers (CGNAT), offices and universities put many people
# behind one IP. A dashboard viewer uses ~30 req/min, so 1200 ≈ 40 viewers per IP.
RATE_LIMIT_PER_MIN = _env("RATE_LIMIT_PER_MIN", 1200, int)
# Concurrent SSE connections allowed per client IP and in total.
SSE_MAX_PER_CLIENT = _env("SSE_MAX_PER_CLIENT", 50, int)
SSE_MAX_TOTAL = _env("SSE_MAX_TOTAL", 5000, int)
# gzip responses larger than this many bytes (SSE streams are never compressed).
GZIP_MIN_SIZE = _env("GZIP_MIN_SIZE", 500, int)
# /api/simulate is allowed only from this machine (direct loopback, not via a proxy/tunnel)
# or with header `X-Admin-Token: <ADMIN_TOKEN>` when ADMIN_TOKEN is set.
ADMIN_TOKEN = _env("ADMIN_TOKEN", "")

# --- Break ("pauza") screen detection ----------------------------------------
# During breaks the broadcast shows a large blue oval with the Assembly building and a gold
# rim. Measured on the live stream: blue inside the oval 44 % (normal frames <= 4 %),
# gold rim 40 % (normal frames <= 5 %).
BREAK_DETECTION_ENABLED = _env("BREAK_DETECTION_ENABLED", True, bool)
BREAK_MIN_BLUE_FRACTION = _env("BREAK_MIN_BLUE_FRACTION", 0.25, float)
BREAK_MIN_GOLD_FRACTION = _env("BREAK_MIN_GOLD_FRACTION", 0.20, float)
BREAK_ENTER_FRAMES = _env("BREAK_ENTER_FRAMES", 3, int)
BREAK_EXIT_FRAMES = _env("BREAK_EXIT_FRAMES", 2, int)

# --- Next session ------------------------------------------------------------
# The channel does not schedule streams in advance, so the next session time can be set
# manually (admin API) or here; a scheduled ("upcoming") YouTube stream overrides it.
NEXT_SESSION_AT = _env("NEXT_SESSION_AT", "")  # ISO-8601, e.g. 2026-11-19T10:00:00+01:00
CHANNEL_STREAMS_URL = _env("CHANNEL_STREAMS_URL", "https://www.youtube.com/@ParlamentSrbija/streams")
UPCOMING_CHECK_INTERVAL_SEC = _env("UPCOMING_CHECK_INTERVAL_SEC", 900.0, float)

# --- Deployment mode -----------------------------------------------------------
#  standalone : everything on one machine (OCR + website), the original setup
#  worker     : OCR on this machine; pushes state OUT to a relay; accepts no connections
#               from the internet (the local dashboard still works on 127.0.0.1)
#  relay      : public website/API only, fed by a worker; no OCR, no yt-dlp, no PyTorch
MODE = _env("MODE", "standalone").strip().lower()
# worker: base URL of the relay, e.g. https://jelpricajos.example.rs
RELAY_URL = _env("RELAY_URL", "").rstrip("/")
# both sides: shared secret the worker sends as "Authorization: Bearer <token>"
RELAY_TOKEN = _env("RELAY_TOKEN", "")
# worker: push at least this often even when nothing changed (lets the relay detect outages)
RELAY_HEARTBEAT_SEC = _env("RELAY_HEARTBEAT_SEC", 5.0, float)
# worker: also push the annotated OCR debug image this often (0 = never)
RELAY_FRAME_PUSH_SEC = _env("RELAY_FRAME_PUSH_SEC", 10.0, float)
# relay: treat the worker as offline after this long without a push
RELAY_STALE_SEC = _env("RELAY_STALE_SEC", 30.0, float)
# relay: largest accepted update / debug image
RELAY_MAX_BODY_BYTES = _env("RELAY_MAX_BODY_BYTES", 2_000_000, int)
