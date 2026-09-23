"""Cues: reading them, re-cutting them, and stopping them at a mark.

The two behaviours worth asserting are the ones that were got wrong first:
a transcriber's long segment has to become several short cues, and a cue
crossing a mark has to be split rather than attributed to one side.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.chapters.transcripts import (
    Cue,
    Word,
    clip,
    cues_from_words,
    parse_srt,
    total_text,
    trim_to_marks,
    write_windows,
)

SRT = """1
00:00:01,000 --> 00:00:03,500
The harbour is quiet tonight

2
00:00:04,000 --> 00:00:06,000
<i>Too quiet,</i> he said

3
00:00:58,000 --> 00:01:02,000
{\\an8}We should have taken the ferry
"""


def test_a_subtitle_file_becomes_cues() -> None:
    cues = parse_srt(SRT)
    assert len(cues) == 3
    assert cues[0].start_s == pytest.approx(1.0)
    assert cues[0].end_s == pytest.approx(3.5)
    assert cues[1].text == "Too quiet, he said"
    assert cues[2].text == "We should have taken the ferry"


def test_a_block_with_no_text_is_dropped_rather_than_carried() -> None:
    assert parse_srt("7\n00:00:01,000 --> 00:00:02,000\n\n") == []


def test_the_other_decimal_separator_reads_the_same() -> None:
    a = parse_srt("1\n00:00:01,250 --> 00:00:02,000\nhello\n")
    b = parse_srt("1\n00:00:01.250 --> 00:00:02.000\nhello\n")
    assert a == b


# ---------------------------------------------------------------- re-cutting
def words(pairs: list[tuple[float, str]], length: float = 0.4) -> list[Word]:
    return [Word(at, at + length, text) for at, text in pairs]


def test_a_long_segment_becomes_several_cues() -> None:
    """The fix that made everything downstream possible."""
    stream = words([(i * 0.5, f"word{i}") for i in range(60)])
    cues = cues_from_words(stream, target_s=8.0)
    assert len(cues) > 3
    assert all(cue.end_s - cue.start_s <= 8.5 for cue in cues)
    assert total_text(cues).split() == [f"word{i}" for i in range(60)]


def test_a_silence_ends_a_cue_however_short_it_is() -> None:
    stream = words([(0.0, "one"), (0.5, "two"), (9.0, "three")])
    cues = cues_from_words(stream, target_s=60.0, gap_s=1.0)
    assert [cue.text for cue in cues] == ["one two", "three"]


def test_no_words_is_no_cues() -> None:
    assert cues_from_words([]) == []


# ------------------------------------------------------------------ clipping
def test_a_cue_inside_the_span_is_kept_whole() -> None:
    cue = Cue(10.0, 12.0, "inside the chapter")
    assert clip([cue], 5.0, 20.0) == [cue]


def test_a_cue_outside_the_span_is_dropped() -> None:
    assert clip([Cue(1.0, 2.0, "before")], 5.0, 20.0) == []


def test_a_cue_straddling_a_mark_is_split_in_proportion() -> None:
    """The finding: a cue that crosses a mark belongs to both sides."""
    cue = Cue(0.0, 10.0, "one two three four five six seven eight nine ten")
    before = clip([cue], 0.0, 5.0)
    after = clip([cue], 5.0, 10.0)
    assert before[0].text == "one two three four five"
    assert after[0].text == "six seven eight nine ten"


def test_a_cue_that_overlaps_at_all_contributes_a_word() -> None:
    """A mark landing mid-sentence must not silently empty a window."""
    cue = Cue(0.0, 10.0, "one two three four five six seven eight nine ten")
    assert clip([cue], 9.9, 20.0)[0].text


def test_splitting_at_every_mark_keeps_every_word_once() -> None:
    cues = [Cue(0.0, 30.0, " ".join(f"w{i}" for i in range(30)))]
    trimmed = trim_to_marks(cues, [10.0, 20.0])
    assert len(trimmed) == 3
    assert total_text(trimmed).split() == [f"w{i}" for i in range(30)]


def test_trimming_twice_changes_nothing() -> None:
    cues = [Cue(0.0, 30.0, " ".join(f"w{i}" for i in range(30)))]
    once = trim_to_marks(cues, [10.0, 20.0])
    assert trim_to_marks(once, [10.0, 20.0]) == once


def test_a_mark_outside_a_cue_leaves_it_alone() -> None:
    cues = [Cue(0.0, 5.0, "a b c")]
    assert trim_to_marks(cues, [40.0]) == cues


# ------------------------------------------------------------------- writing
def test_an_unchanged_file_is_not_rewritten(tmp_path: Path) -> None:
    """The modification time is load-bearing further along."""
    target = tmp_path / "windows.txt"
    assert write_windows(target, "one\ntwo\n") is True
    stamp = target.stat().st_mtime_ns
    assert write_windows(target, "one\ntwo\n") is False
    assert target.stat().st_mtime_ns == stamp


def test_a_changed_file_is_rewritten(tmp_path: Path) -> None:
    target = tmp_path / "deeper" / "windows.txt"
    write_windows(target, "one\n")
    assert write_windows(target, "two\n") is True
    assert target.read_text(encoding="utf-8") == "two\n"


def test_nothing_partial_is_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    write_windows(target, "one\n")
    assert [p.name for p in tmp_path.iterdir()] == ["windows.txt"]
