"""Offline tests for name matching, classification and the debounce state machine.
Run: python -m pytest tests -q"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from ocr_engine import (  # noqa: E402
    OTHER_OR_NONE, PARTY_MEMBER_SPEAKING, VUCIC_SPEAKING, analyze_detections, name_match_score,
    normalize_text,
)
from stream_monitor import SpeakerStateMachine  # noqa: E402

H = 720


def box(y, h=30, x=10, w=300):
    return (x, y, x + w, y + h)


def test_normalize_handles_latin_and_confusions():
    assert normalize_text("Aleksandar Vučić") == normalize_text("Александар Вучић")
    assert normalize_text("АЛЕКСАНДАР  ВУЧИЂ") == normalize_text("александар вучић")
    # Latin look-alike glyphs mixed into a Cyrillic word
    assert normalize_text("Aлeкcaндap") == normalize_text("Александар")


def test_match_scores():
    assert name_match_score("Александар Вучић") >= 0.95
    assert name_match_score("Александар Вучић - Србија побеђује") >= 0.95
    assert name_match_score("АЛЕКСАНДАР ВУЧИЋ - СРБИЈА ПОБЕЂУЈЕ") >= 0.95
    assert name_match_score("Алексадар Вучиh") >= config.FUZZY_THRESHOLD   # OCR noise
    assert name_match_score("АлександарВучић") >= config.FUZZY_THRESHOLD    # missing space
    assert name_match_score("Александар") < config.FUZZY_THRESHOLD
    assert name_match_score("Александар Мартиновић") < config.FUZZY_THRESHOLD
    assert name_match_score("Ана Брнабић") < config.FUZZY_THRESHOLD
    assert name_match_score("Мила Вучић") < config.FUZZY_THRESHOLD


def test_classification_counts_lines():
    vucic = [(box(10), "Александар Вучић", 0.9),
             (box(50, 26), "Александар Вучић - Србија побеђује", 0.8)]
    assert analyze_detections(vucic, H).classification == VUCIC_SPEAKING

    party = [(box(10), "Милош Вучевић", 0.9),
             (box(50, 26), "Александар Вучић - Србија побеђује", 0.8)]
    assert analyze_detections(party, H).classification == PARTY_MEMBER_SPEAKING

    other = [(box(10), "Мирослав Алексић", 0.9), (box(50, 26), "Народни покрет Србије", 0.8)]
    assert analyze_detections(other, H).classification == OTHER_OR_NONE


def test_words_split_into_boxes_are_merged_per_line():
    dets = [(box(10, x=10, w=150), "Александар", 0.9), (box(12, x=170, w=90), "Вучић", 0.9),
            (box(50, 26), "Александар Вучић - Србија побеђује", 0.8)]
    a = analyze_detections(dets, H)
    assert a.match_count == 2 and a.classification == VUCIC_SPEAKING


def test_size_filter_rejects_tiny_and_huge_text():
    tiny = int(0.02 * H)
    huge = int(0.09 * H)
    dets = [(box(10, tiny), "Александар Вучић", 0.9), (box(60, huge), "Александар Вучић", 0.9)]
    a = analyze_detections(dets, H)
    assert a.match_count == 0 and not any(d.size_ok for d in a.detections)


def run(sm, seq):
    out = []
    for i, raw in enumerate(seq):
        tr = sm.update(raw, float(i))
        if tr:
            out.append((i, tr.prev, tr.new, tr.since))
    return out


V, P, N = VUCIC_SPEAKING, PARTY_MEMBER_SPEAKING, OTHER_OR_NONE


def test_debounce_enter_needs_two_frames():
    sm = SpeakerStateMachine(2, 3, 2)
    assert run(sm, [V, N, V, N]) == []
    sm = SpeakerStateMachine(2, 3, 2)
    assert run(sm, [N, V, V]) == [(2, N, V, 1.0)]  # start time = first supporting frame


def test_debounce_exit_vucic_needs_three_frames():
    sm = SpeakerStateMachine(2, 3, 2)
    trs = run(sm, [V, V, N, N, V, N, P, N])
    assert trs == [(1, N, V, 0.0), (7, V, N, 5.0)]


def test_party_then_vucic():
    sm = SpeakerStateMachine(2, 3, 2)
    trs = run(sm, [P, P, V, V, P, P, P])
    assert [t[2] for t in trs] == [P, V, P]


def test_oversized_overlay_marks_frame_occluded():
    dets = [(box(10, int(0.08 * H)), "Вера Радовић", 0.3),
            (box(70, int(0.057 * H)), "Александар Вучић - Србија побеђује", 0.8)]
    a = analyze_detections(dets, H)
    assert a.occluded and a.classification == PARTY_MEMBER_SPEAKING
    assert not analyze_detections(dets[1:], H).occluded


def test_speech_store_resumes_speech_after_short_gap(tmp_path):
    import asyncio
    from stream_monitor import SpeechStore

    async def scenario():
        store = SpeechStore(tmp_path / "s.db")
        await store.open()
        sid = await store.start(1000.0, "vid", "t")
        await store.update(sid, 1100.0, 1000.0, active=False)
        assert await store.resume_recent(1300.0, 60) is None          # gap 200 s: new speech
        assert await store.resume_recent(1130.0, 60) == (sid, 1000.0)  # gap 30 s: same speech
        await store.update(sid, 1200.0, 1000.0, active=False)
        rows = await store.list_speeches(10)
        await store.close()
        return rows

    rows = asyncio.run(scenario())
    assert len(rows) == 1 and rows[0]["duration_sec"] == 200.0


# --- Real failure (Ana Brnabić, 18 Nov 2026): the large animated chyron repeats the party
# line. While it scales in, its text passes the size filter, so the frame contains the party
# line twice and the old "2 matches = Vučić" rule fired VUCIC_SPEAKING for a party member.

def cap(name, party, y, h=27, x=240, gap=2):
    """Two-line caption block: name line on top, party line right below, left-aligned."""
    return [(box(y, h, x=x, w=200), name, 0.9), (box(y + h + gap, h, x=x, w=380), party, 0.9)]


PARTY = "Александар Вучић - Србија побеђује"


def test_party_line_shown_twice_is_not_vucic():
    dets = cap("Ана Брнабић", PARTY, 300, h=30, x=60) + cap("Ана Брнабић", PARTY, 330)
    a = analyze_detections(dets, H)
    assert a.classification == PARTY_MEMBER_SPEAKING


def test_vucic_caption_with_overlay_is_still_vucic():
    dets = cap("Александар Вучић", PARTY, 300, h=30, x=60) + cap("Александар Вучић", PARTY, 330)
    assert analyze_detections(dets, H).classification == VUCIC_SPEAKING


def test_truncated_party_line_below_other_name_is_not_vucic():
    # the right part of the party line is hidden, so it reads like a bare name
    dets = cap("Ана Брнабић", "Александар Вучић", 330)
    a = analyze_detections(dets, H)
    assert a.classification == PARTY_MEMBER_SPEAKING


def test_bare_name_without_second_line_is_uncertain():
    a = analyze_detections([(box(330, 27, x=240), "Александар Вучић", 0.9)], H)
    assert a.uncertain and a.classification != VUCIC_SPEAKING


def test_break_screen_debounce():
    import asyncio
    from stream_monitor import StreamMonitor

    async def scenario():
        m = StreamMonitor()
        seen = []
        for i, brk in enumerate([True, True, False, True, True, True, False, True, False, False]):
            await m._update_break(brk, float(i))
            seen.append(m.on_break)
        return seen, m

    seen, m = asyncio.run(scenario())
    #  a single non-break frame resets the entry streak; 2 non-break frames are needed to leave
    assert seen == [False, False, False, False, False, True, True, True, True, False]
    assert m.break_since is None


def test_parse_iso_local_and_offset():
    from datetime import datetime, timezone
    from stream_monitor import parse_iso
    assert parse_iso("2026-11-19T10:00:00+01:00") == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc).timestamp()
    assert parse_iso("2026-11-19T10:00") is not None
    assert parse_iso("sutra u deset") is None
