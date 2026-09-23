"""Windows: sized by the chapter, sampled across it, and refused when thin.

The headline test is the one about the long chapter. A flat character cap
that keeps the head and the tail elides the middle, and the middle of a
long chapter is what the chapter is about; that shortcut produces a name
taken from the wrong part of it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import pytest
from mkvkit.chapters.transcripts import Cue
from mkvkit.chapters.windows import (
    ELISION,
    MAX_WINDOW_CHARS,
    MIN_WINDOW_CHARS,
    NO_DIALOGUE,
    budget_for,
    build_windows,
    refusals,
)
from mkvkit.chapters.xml import Chapter, ChapterSet

SECOND = 1_000_000_000
MARKS_S = (0.0, 300.0, 900.0, 1500.0)
RUNTIME_S = 2100.0


def marks(starts=MARKS_S) -> ChapterSet:
    return ChapterSet(tuple(Chapter(round(t * SECOND)) for t in starts))


def talk(start: float, end: float, every: float = 5.0, word: str = "word") -> list[Cue]:
    out = []
    at = start
    while at < end:
        out.append(Cue(at, at + 2.0, f"{word} at {int(at)} seconds of talking here"))
        at += every
    return out


# -------------------------------------------------------------------- sizing
def test_the_budget_follows_the_chapter_not_a_flat_number() -> None:
    short = budget_for(120.0)
    long = budget_for(34 * 60.0)
    assert short == MIN_WINDOW_CHARS
    assert long == MAX_WINDOW_CHARS
    assert budget_for(600.0) > short


def test_the_budget_never_leaves_the_bounds() -> None:
    assert budget_for(0.0) == MIN_WINDOW_CHARS
    assert budget_for(10 * 3600.0) == MAX_WINDOW_CHARS


# ------------------------------------------------------------------- building
def test_one_window_per_mark_and_the_last_runs_to_the_runtime() -> None:
    built = build_windows(talk(0.0, RUNTIME_S), marks(), runtime_s=RUNTIME_S)
    assert len(built) == 4
    assert [w.index for w in built] == [1, 2, 3, 4]
    assert built[3].end_s == pytest.approx(RUNTIME_S)


def test_a_window_with_nothing_said_says_so() -> None:
    cues = talk(0.0, 300.0) + talk(900.0, RUNTIME_S)
    built = build_windows(cues, marks(), runtime_s=RUNTIME_S)
    assert built[1].text == NO_DIALOGUE
    assert built[1].has_dialogue is False
    assert built.silent == (2,)


def test_a_short_chapter_is_never_cut() -> None:
    built = build_windows(talk(0.0, 300.0, every=20.0), marks(), runtime_s=RUNTIME_S)
    assert built[0].sampled is False
    assert ELISION not in built[0].text


def test_a_long_chapter_is_sampled_across_its_whole_span() -> None:
    """Every part of a long chapter is represented, including the middle."""
    dense = [
        Cue(at, at + 1.0, f"minute {int(at // 60)} " + "filler " * 20)
        for at in range(300, 900, 5)
    ]
    built = build_windows(dense, marks(), runtime_s=RUNTIME_S)
    window = built[1]
    assert window.sampled is True
    assert ELISION in window.text
    for minute in (5, 9, 13):
        assert f"minute {minute}" in window.text


def test_the_marks_are_carried_through_untouched() -> None:
    built = build_windows(talk(0.0, RUNTIME_S), marks(), runtime_s=RUNTIME_S)
    assert [w.start_s for w in built] == pytest.approx(list(MARKS_S))


def test_no_marks_is_no_windows() -> None:
    assert len(build_windows(talk(0.0, 60.0), [], runtime_s=RUNTIME_S)) == 0


def test_the_rendering_carries_no_timestamp() -> None:
    """A name with a timecode in it could then only have come from one shown."""
    built = build_windows(
        talk(0.0, RUNTIME_S), marks(), runtime_s=RUNTIME_S,
        names_language="deu", transcript_language="eng", source="a subtitle track",
    )
    text = built.render()
    assert "NAMES MUST BE WRITTEN IN: deu" in text
    assert "[1]" in text and "[4]" in text
    assert ":00:" not in text
    assert "00:05:00" not in text


# ------------------------------------------------------------------ refusals
def test_a_stub_transcript_is_refused() -> None:
    problems = refusals(talk(0.0, 100.0, every=20.0), runtime_s=RUNTIME_S)
    assert any("cue" in p for p in problems)


def test_a_transcript_that_stops_early_is_refused() -> None:
    """It looks well-formed and is not there, which is the dangerous shape."""
    problems = refusals(talk(0.0, 800.0, every=2.0), runtime_s=RUNTIME_S)
    assert any("stops at" in p for p in problems)


def test_a_full_transcript_is_accepted() -> None:
    assert refusals(talk(0.0, RUNTIME_S, every=2.0), runtime_s=RUNTIME_S) == []


def test_no_runtime_is_itself_a_refusal() -> None:
    assert refusals(talk(0.0, 2000.0, every=2.0), runtime_s=0.0)
