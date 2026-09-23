"""Where a chapter list comes from, and which of the three jobs it is.

The adapter is exercised against a dictionary. Nothing in this file opens a
socket, and the test that matters most is the one asserting that it cannot:
a scraped archive that is reachable through a default is reachable by
accident.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.chapters.grid import RATE_RATIO
from mkvkit.chapters.sources import (
    BUCKET_A,
    BUCKET_B,
    BUCKET_C,
    NOT_USABLE,
    Candidate,
    ChapterDatabase,
    ChapterSource,
    SourceDisabled,
    classify,
    named_fraction,
    parse_duration,
    pick,
    title_variants,
)
from mkvkit.chapters.xml import Chapter, ChapterError, ChapterSet, is_generic_name

SECOND = 1_000_000_000
MARKS_S = (0.0, 121.0, 305.0, 640.0, 913.0, 1320.0, 1802.0, 2400.0)
NAMES = (
    "The Harbour at Dawn", "A Letter from the Coast", "The Long Drive North",
    "Rain on the Quarry Road", "What the Foreman Knew", "The Last Ferry",
    "Two Days in the Valley House", "End Credits",
)


def chapter_set(starts=MARKS_S, names=None, source=""):
    return ChapterSet(
        tuple(
            Chapter(round(start * SECOND), None if names is None else names[i])
            for i, start in enumerate(starts)
        ),
        source=source,
    )


# ------------------------------------------------------------------ the counting
def test_a_list_of_timecodes_is_not_a_list_of_names() -> None:
    """The guard that matters: lists like these do get written into files."""
    timecodes = chapter_set(names=tuple(f"00:{i:02d}:11" for i in range(8)))
    assert named_fraction(timecodes) == 0.0
    assert classify(chapter_set(), timecodes).bucket == BUCKET_C


def test_a_label_in_another_language_is_still_a_label() -> None:
    for spelling in ("Capitolo 1", "Hoofdstuk 4", "Chapter One", "Kapitel 12"):
        assert is_generic_name(spelling) is True


def test_a_name_that_merely_looks_like_a_label_is_a_name() -> None:
    for name in ("Chapter and Verse", "Seven", "Part of the Bargain"):
        assert is_generic_name(name) is False


# ----------------------------------------------------------------- the three jobs
def test_names_onto_marks_you_already_have_is_bucket_b() -> None:
    found = classify(chapter_set(), chapter_set(names=NAMES))
    assert found.bucket == BUCKET_B
    assert found.usable and not found.needs_a_person
    assert found.named == 8


def test_a_file_with_no_marks_takes_both_and_wants_a_person() -> None:
    found = classify(
        None, chapter_set(names=NAMES), runtime_s=2700.0, candidate_runtime_s=2690.0
    )
    assert found.bucket == BUCKET_A
    assert found.needs_a_person


def test_grids_that_disagree_are_never_best_effort_aligned() -> None:
    other = chapter_set(tuple(t + 40.0 * i for i, t in enumerate(MARKS_S)))
    found = classify(chapter_set(), other.renamed(NAMES))
    assert found.bucket == NOT_USABLE
    assert "do not agree" in found.reason


def test_a_rate_converted_list_is_refused_for_a_file_with_no_marks() -> None:
    """The grid agreement was the only evidence, and there is no grid."""
    found = classify(
        None, chapter_set(names=NAMES),
        runtime_s=2700.0, candidate_runtime_s=2700.0 * RATE_RATIO,
    )
    assert found.bucket == NOT_USABLE
    assert "rate conversion" in found.reason


def test_a_runtime_that_fits_nothing_is_refused() -> None:
    found = classify(
        None, chapter_set(names=NAMES), runtime_s=2700.0, candidate_runtime_s=4400.0
    )
    assert found.bucket == NOT_USABLE


def test_an_empty_candidate_is_refused_rather_than_counted() -> None:
    assert classify(chapter_set(), ChapterSet()).bucket == NOT_USABLE


def test_the_safest_job_is_preferred_then_the_most_names() -> None:
    marks = chapter_set()
    thin = chapter_set(names=(*NAMES[:3], None, None, None, None, None))
    full = chapter_set(names=NAMES)
    chosen = pick(
        marks,
        [
            (Candidate("1", "thin", confirmations=9), thin),
            (Candidate("2", "full", confirmations=0), full),
        ],
    )
    assert chosen is not None
    candidate, chapters, found = chosen
    assert candidate.id == "2"
    assert chapters.named_count == 8
    assert found.bucket == BUCKET_B


def test_nothing_usable_is_none_rather_than_a_guess() -> None:
    other = chapter_set(tuple(t + 40.0 * i for i, t in enumerate(MARKS_S)))
    assert pick(chapter_set(), [(Candidate("1", "x"), other.renamed(NAMES))]) is None


# -------------------------------------------------------------- title variations
def test_the_obvious_respellings_are_tried() -> None:
    variants = title_variants("The Quiet Harbour: Second Watch")
    assert "Quiet Harbour: Second Watch" in variants
    assert "The Quiet Harbour" in variants
    assert "Second Watch" in variants


def test_a_sequel_number_is_tried_in_the_other_numbering() -> None:
    assert "Blue Canyon II" in title_variants("Blue Canyon 2")
    assert "Blue Canyon 3" in title_variants("Blue Canyon III")


def test_the_title_itself_is_never_offered_back() -> None:
    assert "Winter Tide" not in title_variants("Winter Tide")


# ----------------------------------------------------------------- the adapter
LISTING = """
<table><tbody>
<tr><td style="width:7%">Movie</td>
    <td><a href="/browse/101">The Quiet Harbour (1978)</a></td>
    <td style="width:12%">1:52.11</td></tr>
<tr><td style="width:7%">Movie</td>
    <td><a href="/browse/102">Quiet Harbour</a></td>
    <td style="width:12%">0:00.00</td></tr>
</tbody></table>
<span class="label confirm green">7</span><span class="label confirm green">1</span>
"""

DOCUMENT = """<?xml version="1.0"?>
<chapterInfo><title>The Quiet Harbour</title>
<chapters>
<chapter time="00:00:00.000" name="The Harbour at Dawn" />
<chapter time="00:02:01.000" name="A Letter &amp; a Warning" />
<chapter time="00:05:05.000" />
</chapters></chapterInfo>
"""


class Recorder:
    """A fetch that answers from a dictionary and records what it was asked."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers) -> str:  # type: ignore[no-untyped-def]
        self.calls.append((url, dict(headers)))
        for key, body in self.pages.items():
            if key in url:
                return body
        raise AssertionError(f"nothing recorded for {url}")


def database(tmp_path: Path, **kwargs) -> tuple[ChapterDatabase, Recorder]:
    fetch = Recorder({"browse?title": LISTING, "101.xml": DOCUMENT})
    slept: list[float] = []
    source = ChapterDatabase(
        base_url="https://example.com",
        fetch=fetch,
        cache_dir=tmp_path / "cache",
        sleep=slept.append,
        **kwargs,
    )
    return source, fetch


def test_the_adapter_satisfies_the_protocol(tmp_path: Path) -> None:
    source, _ = database(tmp_path)
    assert isinstance(source, ChapterSource)


def test_it_does_nothing_until_it_is_enabled(tmp_path: Path) -> None:
    """A scraped archive reachable through a default is reachable by accident."""
    source, fetch = database(tmp_path)
    with pytest.raises(SourceDisabled):
        source.search("The Quiet Harbour")
    with pytest.raises(SourceDisabled):
        source.get("101")
    assert fetch.calls == []


def test_a_listing_becomes_candidates(tmp_path: Path) -> None:
    source, _ = database(tmp_path, enabled=True)
    found = source.search("The Quiet Harbour")
    assert [c.id for c in found] == ["101", "102"]
    assert found[0].year == 1978
    assert found[0].runtime_s == pytest.approx(6731.0)
    assert found[0].confirmations == 7
    assert found[0].source == source.name


def test_a_blank_duration_is_no_answer_rather_than_zero() -> None:
    assert parse_duration("0:00.00") is None
    assert parse_duration("") is None
    assert parse_duration("1:52:11") == pytest.approx(6731.0)


def test_a_document_becomes_marks_and_names(tmp_path: Path) -> None:
    source, _ = database(tmp_path, enabled=True)
    chapters = source.get("101")
    assert len(chapters) == 3
    assert chapters.starts_s == pytest.approx((0.0, 121.0, 305.0))
    assert chapters.names[1] == "A Letter & a Warning"
    assert chapters.names[2] is None
    assert chapters.source == "The Quiet Harbour"


def test_a_document_with_no_chapters_is_an_error(tmp_path: Path) -> None:
    source = ChapterDatabase(
        base_url="https://example.com",
        fetch=lambda url, headers: "<chapterInfo/>",
        enabled=True,
    )
    with pytest.raises(ChapterError):
        source.get("404")


def test_the_second_ask_costs_no_request(tmp_path: Path) -> None:
    source, fetch = database(tmp_path, enabled=True)
    source.get("101")
    source.get("101")
    assert len(fetch.calls) == 1
    assert source.cache_hits == 1


def test_the_cache_answers_even_when_the_source_is_off(tmp_path: Path) -> None:
    """A survey re-run offline is the normal case, not an edge case."""
    hot, _ = database(tmp_path, enabled=True)
    hot.get("101")
    cold, fetch = database(tmp_path)
    assert len(cold.get("101")) == 3
    assert fetch.calls == []


def test_requests_are_spaced_and_identified(tmp_path: Path) -> None:
    ticks = iter([0.0, 0.0, 0.1, 0.1, 10.0, 10.0])
    fetch = Recorder({"browse?title": LISTING, "101.xml": DOCUMENT})
    slept: list[float] = []
    source = ChapterDatabase(
        base_url="https://example.com", fetch=fetch, enabled=True,
        sleep=slept.append, clock=lambda: next(ticks), min_interval_s=0.6,
    )
    source.search("The Quiet Harbour")
    source.get("101")
    assert slept and slept[0] == pytest.approx(0.5)
    assert all("mkvkit" in headers["User-Agent"] for _, headers in fetch.calls)
