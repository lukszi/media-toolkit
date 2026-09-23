"""Grid matching: the marks, both rate directions, and the rule about names.

The last two tests use the two generated chapter fixtures, which are the same
twelve marks with one of them rate-converted -- a known answer for a
comparison whose whole job is to recognise that.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.chapters.grid import (
    COUNT_MISMATCH,
    DIFFERS,
    LOOSE,
    MATCH,
    RATE_RATIO,
    TOO_FEW,
    adopt,
    copy_names,
    grid_match,
    runtime_match,
)
from mkvkit.chapters.xml import Chapter, ChapterError, ChapterSet

SECOND = 1_000_000_000
MARKS_S = (0.0, 120.0, 300.0, 640.0, 910.0, 1320.0, 1800.0, 2400.0)


def chapter_set(starts: tuple[float, ...], names: tuple[str | None, ...] | None = None
                ) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(round(start * SECOND), None if names is None else names[i])
            for i, start in enumerate(starts)
        )
    )


# ---------------------------------------------------------------------- matching
def test_the_same_grid_matches_exactly() -> None:
    match = grid_match(MARKS_S, MARKS_S)
    assert match.verdict == MATCH
    assert match.scale == 1.0
    assert match.max_deviation_s == pytest.approx(0.0)
    assert match.usable_for_names


def test_a_constant_offset_is_not_a_disagreement() -> None:
    """One list starts at the first frame, the other after a logo."""
    shifted = tuple(t + 8.0 for t in MARKS_S)
    match = grid_match(MARKS_S, shifted)
    assert match.verdict == MATCH
    assert match.offset_s == pytest.approx(-8.0)
    assert match.max_deviation_s == pytest.approx(0.0)


def test_a_rate_converted_candidate_is_recognised() -> None:
    converted = tuple(t * RATE_RATIO for t in MARKS_S)
    match = grid_match(MARKS_S, converted)
    assert match.verdict == MATCH
    assert match.rate_converted
    assert match.scale == pytest.approx(1.0 / RATE_RATIO)
    assert match.max_deviation_s == pytest.approx(0.0, abs=1e-6)


def test_the_conversion_is_recognised_in_the_other_direction_too() -> None:
    converted = tuple(t / RATE_RATIO for t in MARKS_S)
    match = grid_match(MARKS_S, converted)
    assert match.verdict == MATCH
    assert match.scale == pytest.approx(RATE_RATIO)


def test_one_mark_slightly_out_is_loose_and_a_long_way_out_differs() -> None:
    near = list(MARKS_S)
    near[4] += 4.0
    assert grid_match(MARKS_S, tuple(near)).verdict == LOOSE
    far = list(MARKS_S)
    far[4] += 40.0
    assert grid_match(MARKS_S, tuple(far)).verdict == DIFFERS


def test_different_counts_are_not_compared_at_all() -> None:
    match = grid_match(MARKS_S, MARKS_S[:-1])
    assert match.verdict == COUNT_MISMATCH
    assert match.max_deviation_s is None
    assert not match.usable_for_names


def test_two_marks_prove_nothing() -> None:
    assert grid_match(MARKS_S[:2], MARKS_S[:2]).verdict == TOO_FEW


def test_a_match_describes_itself_in_one_line() -> None:
    text = str(grid_match(MARKS_S, tuple(t * RATE_RATIO for t in MARKS_S)))
    assert "match" in text
    assert "rate-converted" in text


def test_a_chapter_set_can_be_compared_directly() -> None:
    assert grid_match(chapter_set(MARKS_S), chapter_set(MARKS_S)).verdict == MATCH


# ----------------------------------------------------------------------- runtime
def test_a_runtime_matches_at_any_of_the_scales() -> None:
    assert runtime_match(5400.0, 5400.0)[0] is True
    assert runtime_match(5400.0, 5400.0 * RATE_RATIO)[0] is True
    assert runtime_match(5400.0, 3600.0)[0] is False


# ------------------------------------------------------------------ copying names
def test_names_are_copied_onto_the_file_s_own_marks() -> None:
    mine = chapter_set(MARKS_S)
    theirs = chapter_set(
        tuple(t * RATE_RATIO for t in MARKS_S),
        tuple(f"Name {i}" for i in range(len(MARKS_S))),
    )
    match = grid_match(mine, theirs)
    result = copy_names(mine, theirs, match)
    assert result.starts_ns == mine.starts_ns
    assert result.names[0] == "Name 0"


def test_a_generic_candidate_name_is_dropped_rather_than_written() -> None:
    mine = chapter_set(MARKS_S)
    labels = tuple(f"Chapter {i + 1}" for i in range(len(MARKS_S)))
    theirs = chapter_set(MARKS_S, labels)
    result = copy_names(mine, theirs, grid_match(mine, theirs))
    assert set(result.names) == {None}


def test_names_are_not_copied_from_a_grid_that_does_not_agree() -> None:
    mine = chapter_set(MARKS_S)
    far = list(MARKS_S)
    far[2] += 60.0
    theirs = chapter_set(tuple(far), tuple("abcdefgh"))
    with pytest.raises(ChapterError, match="do not agree"):
        copy_names(mine, theirs, grid_match(mine, theirs))


def test_a_rate_converted_candidate_may_not_be_written_into_an_empty_file() -> None:
    """With no marks to compare against, nothing proves it is the same cut."""
    converted = chapter_set(tuple(t * RATE_RATIO for t in MARKS_S))
    match = grid_match(MARKS_S, converted.starts_s)
    with pytest.raises(ChapterError, match="rate-converted"):
        adopt(converted, match)


def test_an_unscaled_candidate_may_be_adopted() -> None:
    theirs = chapter_set(MARKS_S, tuple("abcdefgh"))
    assert adopt(theirs, grid_match(MARKS_S, MARKS_S)) is theirs
    assert adopt(theirs) is theirs


# -------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_two_chapter_fixtures_are_the_same_grid_at_two_rates(
    media_fixtures: dict[str, Path],
) -> None:
    from mkvkit.probe import probe

    plain = [c.start_s for c in probe(media_fixtures["chapter_grid.mkv"]).chapters]
    converted = [
        c.start_s for c in probe(media_fixtures["chapter_grid_pal.mkv"]).chapters
    ]
    assert len(plain) == len(converted) == 12
    match = grid_match(plain, converted)
    assert match.verdict == MATCH
    assert match.rate_converted
    assert match.max_deviation_s is not None and match.max_deviation_s < 0.01


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_fixture_matches_itself_exactly(media_fixtures: dict[str, Path]) -> None:
    from mkvkit.probe import probe

    marks = [c.start_s for c in probe(media_fixtures["chapter_grid.mkv"]).chapters]
    match = grid_match(marks, marks)
    assert match.verdict == MATCH
    assert match.scale == 1.0
