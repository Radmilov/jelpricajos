"""Frame preprocessing, ROI cropping, OCR and Cyrillic name matching."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np
from rapidfuzz import fuzz

import config

log = logging.getLogger("ocr")

VUCIC_SPEAKING = "VUCIC_SPEAKING"
PARTY_MEMBER_SPEAKING = "PARTY_MEMBER_SPEAKING"
OTHER_OR_NONE = "OTHER_OR_NONE"

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# Latin glyphs that OCR commonly emits in place of visually identical Cyrillic ones.
_LOOKALIKE = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "i": "і",
    "n": "п", "r": "г",
})
# Serbian Latin -> Cyrillic transliteration (used when a word is mostly Latin).
_LATIN_DIGRAPHS = [("dž", "џ"), ("lj", "љ"), ("nj", "њ"), ("dj", "ђ")]
_LATIN_SINGLE = str.maketrans({
    "a": "а", "b": "б", "c": "ц", "č": "ч", "ć": "ћ", "d": "д", "đ": "ђ", "e": "е",
    "f": "ф", "g": "г", "h": "х", "i": "и", "j": "ј", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "š": "ш", "t": "т", "u": "у",
    "v": "в", "z": "з", "ž": "ж", "q": "к", "w": "в", "x": "кс", "y": "ј",
})
# Cyrillic confusions: fold visually similar letters together.
_CYR_FOLD = str.maketrans({
    "ђ": "ћ", "ħ": "ћ", "һ": "ћ", "і": "и", "ї": "и", "й": "и", "ё": "е",
    "ъ": "ь", "ы": "ь",
})
_LATIN_RE = re.compile(r"[a-zčćđšž]")
_CYR_RE = re.compile(r"[Ѐ-ӿ]")
_NON_WORD = re.compile(r"[^\w\s-]", re.UNICODE)


def _normalize_word(word: str) -> str:
    if not _LATIN_RE.search(word):
        return word
    if _CYR_RE.search(word):
        # Mixed script: stray Latin glyphs are OCR look-alikes of Cyrillic letters.
        return word.translate(_LOOKALIKE)
    # Pure Latin word: Serbian Latin transliteration.
    for src, dst in _LATIN_DIGRAPHS:
        word = word.replace(src, dst)
    return word.translate(_LATIN_SINGLE)


def normalize_text(text: str) -> str:
    """Lower-case, unify scripts/confusable glyphs and collapse whitespace."""
    text = (text or "").lower().replace("_", " ")
    text = _NON_WORD.sub(" ", text)
    words = [_normalize_word(w) for w in text.split()]
    return " ".join(words).translate(_CYR_FOLD).strip()


_TARGET = normalize_text(config.TARGET_NAME)
_TARGET_NOSPACE = _TARGET.replace(" ", "")
_SURNAME = _TARGET.split()[-1]


def name_match_score(text: str) -> float:
    """Return a 0..1 similarity between `text` and the target name.

    If the text is longer than the name (e.g. "Александар Вучић - Србија побеђује")
    the best-aligned substring is scored; otherwise the full strings are compared,
    so a lone "Александар" never counts as a match.
    """
    norm = normalize_text(text)
    if not norm:
        return 0.0
    if len(norm) >= len(_TARGET):
        score = max(fuzz.partial_ratio(_TARGET, norm),
                    fuzz.partial_ratio(_TARGET_NOSPACE, norm.replace(" ", "")))
    else:
        score = fuzz.ratio(_TARGET, norm)
    # Guard: the surname itself must be (fuzzily) present, so "Александар <someone else>"
    # can never reach the threshold on the first name alone.
    if not _surname_present(norm):
        score = min(score, 50.0)
    return score / 100.0


def name_only_score(text: str) -> float:
    """0..1 similarity of the WHOLE line to the bare name (no party suffix allowed).
    "Александар Вучић" -> ~1.0, "Александар Вучић - Србија побеђује" -> low."""
    norm = normalize_text(text).replace("-", " ")
    norm = " ".join(norm.split())
    if not norm or not _surname_present(norm):
        return 0.0
    return max(fuzz.ratio(_TARGET, norm), fuzz.ratio(_TARGET_NOSPACE, norm.replace(" ", ""))) / 100.0


def _surname_present(norm: str) -> bool:
    for word in norm.replace("-", " ").split():
        if fuzz.ratio(_SURNAME, word) >= 70:
            return True
        # Words glued together by OCR ("александарвучић"): check the tail.
        if len(word) >= len(_TARGET_NOSPACE) - 2 and fuzz.ratio(_SURNAME, word[-len(_SURNAME):]) >= 70:
            return True
    return False


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    text: str
    confidence: float
    box: Tuple[int, int, int, int]  # x1, y1, x2, y2 in ROI pixel coords (original scale)
    height_px: float
    height_ratio: float  # relative to the full frame height
    size_ok: bool


@dataclass
class TextLine:
    text: str
    height_ratio: float
    match_score: float
    is_match: bool
    box: Tuple[int, int, int, int]
    # "name"  = the bare name as the TOP line of a caption block (=> he is speaking)
    # "party" = his name inside the parliamentary-group line (=> a member of his group)
    # "name?" = bare name with no caption line next to it (ambiguous)
    role: str = ""


@dataclass
class FrameAnalysis:
    classification: str
    match_count: int
    lines: List[TextLine] = field(default_factory=list)
    detections: List[Detection] = field(default_factory=list)
    frame_height: int = 0
    elapsed_ms: float = 0.0
    occluded: bool = False  # oversized overlay text present (animated chyron)
    uncertain: bool = False  # caption structure ambiguous: must not change the state
    source: str = "ocr"     # "ocr" | "reused" (caption unchanged) | "empty" (no text pixels)

    def to_debug(self) -> dict:
        return {
            "classification": self.classification,
            "match_count": self.match_count,
            "occluded": self.occluded,
            "uncertain": self.uncertain,
            "source": self.source,
            "frame_height": self.frame_height,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "lines": [
                {
                    "text": ln.text,
                    "height_pct": round(ln.height_ratio * 100, 2),
                    "match_score": round(ln.match_score, 3),
                    "is_match": ln.is_match,
                    "role": ln.role,
                }
                for ln in self.lines
            ],
            "detections": [
                {
                    "text": d.text,
                    "confidence": round(d.confidence, 3),
                    "height_px": round(d.height_px, 1),
                    "height_pct": round(d.height_ratio * 100, 2),
                    "size_ok": d.size_ok,
                }
                for d in self.detections
            ],
        }


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def roi_bounds(width: int, height: int) -> Tuple[int, int, int, int]:
    x1 = int(round(config.ROI_X_START * width))
    x2 = int(round(config.ROI_X_END * width))
    y1 = int(round(config.ROI_Y_START * height))
    y2 = int(round(config.ROI_Y_END * height))
    return max(0, x1), max(0, y1), min(width, x2), min(height, y2)


def crop_roi(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi_bounds(w, h)
    return frame[y1:y2, x1:x2]


def preprocess(roi_bgr: np.ndarray) -> np.ndarray:
    """Grayscale + CLAHE (+ optional white-text threshold), upscaled for OCR."""
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(
        clipLimit=config.CLAHE_CLIP_LIMIT,
        tileGridSize=(config.CLAHE_TILE_GRID, config.CLAHE_TILE_GRID),
    )
    img = clahe.apply(gray)
    if config.PREPROCESS_MODE == "threshold":
        _, mask = cv2.threshold(img, config.WHITE_TEXT_THRESHOLD, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
        img = cv2.bitwise_not(mask)  # dark text on white background
    if config.OCR_UPSCALE and config.OCR_UPSCALE != 1.0:
        img = cv2.resize(img, None, fx=config.OCR_UPSCALE, fy=config.OCR_UPSCALE,
                         interpolation=cv2.INTER_CUBIC)
    return img


def group_lines(dets: List[Detection]) -> List[List[Detection]]:
    """Group detections that sit on the same text row (vertical overlap)."""
    rows: List[List[Detection]] = []
    for d in sorted(dets, key=lambda d: (d.box[1] + d.box[3]) / 2):
        placed = False
        for row in rows:
            ry1 = min(r.box[1] for r in row)
            ry2 = max(r.box[3] for r in row)
            overlap = min(ry2, d.box[3]) - max(ry1, d.box[1])
            if overlap > 0.5 * min(ry2 - ry1, d.box[3] - d.box[1]):
                row.append(d)
                placed = True
                break
        if not placed:
            rows.append([d])
    return [sorted(r, key=lambda d: d.box[0]) for r in rows]


def classify(match_count: int) -> str:
    """Legacy count-based rule (kept for reference / external callers)."""
    if match_count >= 2:
        return VUCIC_SPEAKING
    if match_count == 1:
        return PARTY_MEMBER_SPEAKING
    return OTHER_OR_NONE


def _neighbour(a: TextLine, b: TextLine) -> bool:
    """True if `b` is the caption line directly below `a` (left-aligned, adjacent)."""
    ha, hb = a.box[3] - a.box[1], b.box[3] - b.box[1]
    h = max(ha, hb, 1)
    gap = b.box[1] - a.box[3]
    return abs(a.box[0] - b.box[0]) <= 1.5 * h and -0.5 * h <= gap <= 1.0 * h


def classify_lines(lines: List[TextLine]) -> Tuple[str, bool]:
    """Classify from the caption's structure instead of counting matches.

    A caption block is two left-aligned lines: speaker name, then parliamentary group.
    Vučić speaks only if a line that is *just his name* is the TOP line of a block. His
    name inside the group line ("… - Србија побеђује") means a member of his group,
    however many times that line is on screen (the animated chyron repeats it).
    Returns (classification, uncertain)."""
    for ln in lines:
        if not ln.is_match:
            continue
        if name_only_score(ln.text) < config.FUZZY_THRESHOLD:
            ln.role = "party"
            continue
        above = any(_neighbour(o, ln) for o in lines if o is not ln)
        below = any(_neighbour(ln, o) for o in lines if o is not ln)
        if above:
            ln.role = "party"   # second line that reads like a bare name = truncated group line
        elif below:
            ln.role = "name"
        else:
            ln.role = "name?"
    roles = {ln.role for ln in lines}
    if "name" in roles:
        return VUCIC_SPEAKING, False
    if "party" in roles:
        return PARTY_MEMBER_SPEAKING, "name?" in roles
    if "name?" in roles:
        return PARTY_MEMBER_SPEAKING, True
    return OTHER_OR_NONE, False


def analyze_detections(raw: List[Tuple[Tuple[int, int, int, int], str, float]],
                       frame_height: int) -> FrameAnalysis:
    """Apply the size filter, merge rows and count target-name matches.

    `raw` holds (box_in_roi_coords, text, confidence) tuples.
    """
    dets: List[Detection] = []
    for box, text, conf in raw:
        h_px = float(box[3] - box[1])
        ratio = h_px / frame_height if frame_height else 0.0
        size_ok = (config.MIN_BOX_HEIGHT_RATIO <= ratio <= config.MAX_BOX_HEIGHT_RATIO
                   and conf >= config.OCR_MIN_CONFIDENCE)
        dets.append(Detection(text=text, confidence=conf, box=box, height_px=h_px,
                              height_ratio=ratio, size_ok=size_ok))

    lines: List[TextLine] = []
    for row in group_lines([d for d in dets if d.size_ok]):
        text = " ".join(d.text for d in row)
        score = name_match_score(text)
        box = (min(d.box[0] for d in row), min(d.box[1] for d in row),
               max(d.box[2] for d in row), max(d.box[3] for d in row))
        lines.append(TextLine(
            text=text,
            height_ratio=float(np.median([d.height_ratio for d in row])),
            match_score=score,
            is_match=score >= config.FUZZY_THRESHOLD,
            box=box,
        ))
    count = sum(1 for ln in lines if ln.is_match)
    occluded = any(d.height_ratio > config.OCCLUSION_MIN_HEIGHT_RATIO and len(d.text.strip()) >= 3
                   for d in dets)
    classification, uncertain = classify_lines(lines)
    return FrameAnalysis(classification=classification, match_count=count,
                         lines=lines, detections=dets, frame_height=frame_height,
                         occluded=occluded, uncertain=uncertain)


def draw_debug(roi_bgr: np.ndarray, analysis: Optional[FrameAnalysis]) -> np.ndarray:
    """Annotate the ROI: green = matched line, yellow = valid-size text,
    red = rejected by the size filter. Labels are indexes into the debug JSON
    (OpenCV can't render Cyrillic)."""
    img = roi_bgr.copy()
    if analysis is None:
        return img
    for i, d in enumerate(analysis.detections):
        x1, y1, x2, y2 = d.box
        color = (0, 220, 255) if d.size_ok else (60, 60, 255)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)
        cv2.putText(img, f"#{i} {d.height_ratio * 100:.1f}%", (x1, max(10, y1 - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)
    for ln in analysis.lines:
        if ln.is_match:
            x1, y1, x2, y2 = ln.box
            cv2.rectangle(img, (x1 - 3, y1 - 3), (x2 + 3, y2 + 3), (60, 230, 60), 2)
    label = f"{analysis.classification}  matches={analysis.match_count}  [{analysis.source}]"
    if analysis.occluded:
        label += "  OCCLUDED"
    cv2.rectangle(img, (0, 0), (min(img.shape[1], 10 + 7 * len(label)), 16), (0, 0, 0), -1)
    cv2.putText(img, label, (4, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (255, 255, 255), 1,
                cv2.LINE_AA)
    return img


def placeholder_image(message: str, width: int = 1024, height: int = 173) -> np.ndarray:
    img = np.full((height, width, 3), 24, np.uint8)
    cv2.putText(img, message, (20, height // 2 + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (170, 170, 170), 2, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------

class CaptionGate:
    """Decides, cheaply, whether a frame needs a full OCR pass."""

    # Masks keep a fixed fraction of the ROI's resolution (not a fixed width): with a wide
    # ROI a fixed width blurred strokes together, so different names of similar length
    # (e.g. "Александар Вучић" vs "Милош Вучевић") looked "unchanged" and OCR was skipped.
    SCALE = 0.6

    def __init__(self) -> None:
        self.mask: Optional[np.ndarray] = None
        self.boxes: List[Tuple[int, int, int, int]] = []  # text boxes of the last OCR, mask coords
        self.at = 0.0

    def bright_mask(self, roi_bgr: np.ndarray) -> np.ndarray:
        h, w = roi_bgr.shape[:2]
        small = cv2.resize(roi_bgr, (max(1, int(w * self.SCALE)), max(1, int(h * self.SCALE))),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        mask = (gray >= config.TEXT_BRIGHT_LEVEL).astype(np.uint8)
        return cv2.dilate(mask, np.ones((3, 3), np.uint8))

    def decide(self, roi_bgr: np.ndarray) -> Tuple[str, np.ndarray]:
        mask = self.bright_mask(roi_bgr)
        if mask.mean() < config.TEXT_MIN_BRIGHT_FRACTION:
            return "empty", mask
        fresh = time.time() - self.at < config.FORCE_OCR_EVERY_SEC
        if self.mask is not None and self.mask.shape == mask.shape and fresh:
            if self.similarity(mask) >= config.CAPTION_REUSE_IOU:
                return "reused", mask
        return "ocr", mask

    def similarity(self, mask: np.ndarray) -> float:
        """How unchanged the caption is, 0..1.

        With text boxes from the last OCR: the LOWEST overlap over those boxes, one text line
        at a time. A whole-mask comparison let a long, unchanged party line hide a changed
        name line (Vučić vs Vučević scored 0.85), and comparing the whole strip also counted
        moving bright background (monitors, shirts). Without boxes (no caption last time):
        whole-mask overlap, which drops as soon as a caption appears."""
        if not self.boxes:
            union = np.logical_or(mask, self.mask).sum()
            return float(np.logical_and(mask, self.mask).sum() / union) if union else 1.0
        worst = 1.0
        for x1, y1, x2, y2 in self.boxes:
            a, b = mask[y1:y2, x1:x2], self.mask[y1:y2, x1:x2]
            union = np.logical_or(a, b).sum()
            if union:
                worst = min(worst, float(np.logical_and(a, b).sum() / union))
        return worst

    def remember(self, mask: np.ndarray, boxes=()) -> None:
        """Store the OCR'd frame's mask and its text boxes (ROI pixel coords)."""
        s, (h, w) = self.SCALE, mask.shape
        self.boxes = [(max(0, int(x1 * s) - 2), max(0, int(y1 * s) - 2),
                       min(w, int(x2 * s) + 2), min(h, int(y2 * s) + 2)) for x1, y1, x2, y2 in boxes]
        self.mask, self.at = mask, time.time()

    def reset(self) -> None:
        self.mask, self.boxes, self.at = None, [], 0.0


class OCREngine:
    """Thin wrapper around EasyOCR (preferred) or pytesseract."""

    def __init__(self) -> None:
        self.backend = config.OCR_BACKEND.lower()
        self._reader = None
        self._lock = threading.Lock()
        self.ready = False
        self.error: Optional[str] = None
        self.gate = CaptionGate()
        self._last: Optional[FrameAnalysis] = None
        self.counts = {"ocr": 0, "reused": 0, "empty": 0}

    def load(self) -> None:
        """Blocking model load. Call from a worker thread."""
        try:
            if self.backend == "tesseract":
                import pytesseract  # noqa: F401
                if config.TESSERACT_CMD:
                    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
                langs = pytesseract.get_languages(config="")
                if config.TESSERACT_LANG not in langs:
                    raise RuntimeError(
                        f"Tesseract language '{config.TESSERACT_LANG}' is not installed "
                        f"(available: {', '.join(langs)})")
            else:
                import easyocr
                if config.OCR_THREADS > 0:
                    import torch
                    torch.set_num_threads(config.OCR_THREADS)
                    cv2.setNumThreads(config.OCR_THREADS)
                log.info("Loading EasyOCR %s (gpu=%s) — first run downloads models…",
                         config.OCR_LANGUAGES, config.OCR_GPU)
                self._reader = easyocr.Reader(config.OCR_LANGUAGES, gpu=config.OCR_GPU,
                                              verbose=False)
            self.ready = True
            self.error = None
            log.info("OCR backend '%s' ready", self.backend)
        except Exception as exc:  # surfaced in /api/state
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("Failed to initialise OCR backend")
            raise

    def _run_easyocr(self, img: np.ndarray):
        results = self._reader.readtext(img, detail=1, paragraph=False,
                                        width_ths=0.8, decoder="greedy")
        out = []
        for pts, text, conf in results:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append(((min(xs), min(ys), max(xs), max(ys)), text, float(conf)))
        return out

    def _run_tesseract(self, img: np.ndarray):
        import pytesseract
        data = pytesseract.image_to_data(img, lang=config.TESSERACT_LANG, config="--psm 6",
                                         output_type=pytesseract.Output.DICT)
        out = []
        for i, text in enumerate(data["text"]):
            text = (text or "").strip()
            conf = float(data["conf"][i])
            if not text or conf < 0:
                continue
            x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
            out.append(((x, y, x + w, y + h), text, conf / 100.0))
        return out

    def analyze_frame(self, frame_bgr: np.ndarray) -> Tuple[FrameAnalysis, np.ndarray]:
        """OCR the chyron ROI of a full frame. Returns (analysis, annotated ROI)."""
        t0 = time.perf_counter()
        roi = crop_roi(frame_bgr)
        mask = None
        if config.OCR_SKIP_ENABLED:
            decision, mask = self.gate.decide(roi)
            if decision == "empty":
                self.counts["empty"] += 1
                self.gate.reset()
                analysis = FrameAnalysis(classification=OTHER_OR_NONE, match_count=0,
                                         frame_height=frame_bgr.shape[0], source="empty",
                                         elapsed_ms=(time.perf_counter() - t0) * 1000)
                return analysis, draw_debug(roi, analysis)
            if decision == "reused" and self._last is not None:
                self.counts["reused"] += 1
                analysis = FrameAnalysis(**{**self._last.__dict__, "source": "reused",
                                            "elapsed_ms": (time.perf_counter() - t0) * 1000})
                return analysis, draw_debug(roi, analysis)
        self.counts["ocr"] += 1
        img = preprocess(roi)
        with self._lock:
            raw = self._run_tesseract(img) if self.backend == "tesseract" else self._run_easyocr(img)
        scale = config.OCR_UPSCALE or 1.0
        scaled = [(
            (int(b[0] / scale), int(b[1] / scale), int(b[2] / scale), int(b[3] / scale)),
            text, conf) for b, text, conf in raw]
        analysis = analyze_detections(scaled, frame_bgr.shape[0])
        analysis.elapsed_ms = (time.perf_counter() - t0) * 1000
        self._last = analysis
        if mask is not None:
            self.gate.remember(mask, [d.box for d in analysis.detections if d.size_ok])
        return analysis, draw_debug(roi, analysis)


__all__ = [
    "OCREngine", "FrameAnalysis", "analyze_detections", "normalize_text", "name_match_score",
    "crop_roi", "preprocess", "draw_debug", "placeholder_image", "classify",
    "VUCIC_SPEAKING", "PARTY_MEMBER_SPEAKING", "OTHER_OR_NONE",
]


# ---------------------------------------------------------------------------
# Break screen ("pauza") detection
# ---------------------------------------------------------------------------

_BREAK_MASKS: dict = {}


def _break_masks(w: int, h: int):
    """Ellipse interior / rim masks fitted to the break graphic (in frame proportions)."""
    key = (w, h)
    if key not in _BREAK_MASKS:
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy, ax, ay = 0.5 * w, 0.448 * h, 0.404 * w, 0.354 * h
        r = ((xx - cx) / ax) ** 2 + ((yy - cy) / ay) ** 2
        _BREAK_MASKS[key] = (r < 0.75, (r > 0.85) & (r < 1.15))
    return _BREAK_MASKS[key]


def break_screen_scores(frame_bgr: np.ndarray) -> Tuple[float, float]:
    """(blue fraction inside the oval, gold fraction on its rim)."""
    small = cv2.resize(frame_bgr, (214, 120), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    blue = (hue >= 105) & (hue <= 130) & (sat >= 110) & (val >= 60) & (val <= 230)
    gold = (hue >= 15) & (hue <= 35) & (sat >= 90) & (val >= 150)
    inner, rim = _break_masks(214, 120)
    return float(blue[inner].mean()), float(gold[rim].mean())


def is_break_screen(frame_bgr: np.ndarray) -> bool:
    blue, gold = break_screen_scores(frame_bgr)
    return blue >= config.BREAK_MIN_BLUE_FRACTION and gold >= config.BREAK_MIN_GOLD_FRACTION
