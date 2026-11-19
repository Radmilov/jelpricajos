"""End-to-end OCR check on rendered chyron frames (needs EasyOCR models; slow).
Run: python -m pytest tests/test_ocr_synthetic.py -q -s"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_engine import (OCREngine, OTHER_OR_NONE, PARTY_MEMBER_SPEAKING,  # noqa: E402
                        VUCIC_SPEAKING)

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

FONT_CANDIDATES = ["C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    pytest.skip("no Cyrillic-capable TTF font found")


def make_frame(top, bottom, W=854, H=480, x=0.28):
    rng = np.random.default_rng(0)
    img = Image.fromarray((rng.random((H, W, 3)) * 60 + 40).astype(np.uint8))
    d = ImageDraw.Draw(img)
    # Layout of the real broadcast caption: left edge ~0.28 W, lines at ~0.87 H and ~0.93 H.
    for text, y in ((top, int(0.872 * H)), (bottom, int(0.932 * H))):
        d.text((int(x * W), y), text, font=font(int(0.042 * H)), fill=(255, 255, 255),
               stroke_width=1, stroke_fill=(30, 30, 30))
    d.text((1100, 20), "УЖИВО", font=font(16), fill=(255, 255, 255))
    return np.array(img)[:, :, ::-1].copy()


@pytest.fixture(scope="module")
def engine():
    e = OCREngine()
    e.load()
    return e


def test_gate_skips_frames_without_caption(engine):
    blank = make_frame("", "")
    analysis, _ = engine.analyze_frame(blank)
    assert analysis.source == "empty" and analysis.classification == OTHER_OR_NONE


def test_gate_reuses_unchanged_caption(engine):
    frame = make_frame("Мирослав Алексић", "Народни покрет Србије")
    first, _ = engine.analyze_frame(frame)
    second, _ = engine.analyze_frame(frame)
    assert first.source == "ocr" and second.source == "reused"
    assert second.classification == first.classification


@pytest.mark.parametrize("top,bottom,expected", [
    ("Александар Вучић", "Александар Вучић - Србија побеђује", VUCIC_SPEAKING),
    ("Милош Вучевић", "Александар Вучић - Србија побеђује", PARTY_MEMBER_SPEAKING),
    ("Мирослав Алексић", "Народни покрет Србије", OTHER_OR_NONE),
])
def test_synthetic_chyron(engine, top, bottom, expected):
    analysis, dbg = engine.analyze_frame(make_frame(top, bottom))
    print("\n", expected, [(l.text, round(l.height_ratio * 100, 2), round(l.match_score, 2))
                           for l in analysis.lines], f"{analysis.elapsed_ms:.0f}ms")
    assert analysis.classification == expected
    assert dbg.ndim == 3


@pytest.mark.parametrize("x", [0.28, 0.025])
def test_caption_position_left_edge(engine, x):
    """19 Nov 2026: the caption moved from ~0.28 W to the bottom-left corner."""
    a, _ = engine.analyze_frame(make_frame("Ана Брнабић", "Александар Вучић - Србија побеђује", x=x))
    assert a.classification == PARTY_MEMBER_SPEAKING
    a, _ = engine.analyze_frame(make_frame("Александар Вучић", "Александар Вучић - Србија побеђује", x=x))
    assert a.classification == VUCIC_SPEAKING


def test_gate_notices_name_change_under_same_party_line(engine):
    """A long, unchanged party line must not hide a changed name line."""
    engine.gate.reset()
    first, _ = engine.analyze_frame(make_frame("Александар Вучић", "Александар Вучић - Србија побеђује", x=0.025))
    second, _ = engine.analyze_frame(make_frame("Милош Вучевић", "Александар Вучић - Србија побеђује", x=0.025))
    assert first.classification == VUCIC_SPEAKING
    assert second.source == "ocr" and second.classification == PARTY_MEMBER_SPEAKING
