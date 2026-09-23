"""The matcher, and the rules a name is held to.

Two things are asserted hardest. A list is never slid into place, however
clearly the evidence says it would fit one mark along -- it is refused. And a
"name" that is really a label never reaches a file, whatever the counter that
produced it thought.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt

from mkvkit.chapters.names import (
    NAMING_RULES,
    NamingRules,
    blank_for,
    check_name,
    check_names,
    match_names,
    provenance,
)
from mkvkit.chapters.verify import MarkEvidence
from mkvkit.chapters.windows import Window
from mkvkit.chapters.xml import Chapter, ChapterSet

SECOND = 1_000_000_000
NAMES = (
    "The Harbour at Dawn",
    "A Letter from the Coast",
    "The Long Drive North",
    "Rain on the Quarry Road",
    "What the Foreman Knew",
    "The Last Ferry",
)
WINDOWS_TEXT = (
    "the harbour is quiet at dawn and the boats have not gone out",
    "a letter came up from the coast this morning addressed to nobody",
    "we drive north all night and the road never seems to end",
    "rain on the quarry road turns the whole hillside into mud",
    "the foreman knew about the pumps and said nothing to anybody",
    "the last ferry leaves at six and we are not going to be on it",
)


def marks(names=None, offset: float = 0.0) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(round((i * 300.0 + offset) * SECOND),
                    None if names is None else names[i])
            for i in range(6)
        )
    )


def windows(texts=WINDOWS_TEXT) -> list[Window]:
    return [
        Window(index=i + 1, start_s=i * 300.0, end_s=(i + 1) * 300.0,
               text=texts[i] or "(no dialogue in this chapter)",
               cues=3 if texts[i] else 0)
        for i in range(len(texts))
    ]


def evidence(names=NAMES, texts=WINDOWS_TEXT) -> list[MarkEvidence]:
    return [
        MarkEvidence(i + 1, i * 300.0, names[i], texts[i]) for i in range(len(names))
    ]


# ---------------------------------------------------------------- single names
def test_a_well_formed_name_passes() -> None:
    assert check_name("The Harbour at Dawn").ok


def test_a_label_is_blocked_whatever_else_is_true() -> None:
    for label in ("Chapter 7", "Kapitel 3", "Chapitre 1", "00:12:34"):
        check = check_name(label)
        assert check.blocked, label


def test_a_timecode_inside_a_name_is_blocked() -> None:
    assert check_name("Arrival at 12:40").blocked


def test_length_and_punctuation_are_notes_rather_than_blocks() -> None:
    """A disc author breaks these routinely and the names are still worth having."""
    check = check_name("A Very Long Name Indeed That Runs Past The Limit.")
    assert not check.ok
    assert not check.blocked
    assert {v.rule for v in check.violations} == {"length", "trailing punctuation"}


def test_strict_makes_every_rule_block() -> None:
    """Which is right for names this pass wrote a moment ago."""
    assert check_name("This one ends in a full stop.", strict=True).blocked


def test_a_name_wrapped_in_quotation_marks_is_flagged() -> None:
    assert any(v.rule == "quotes" for v in check_name('"The Last Ferry"').violations)


def test_a_name_in_the_wrong_language_is_flagged() -> None:
    assert any(
        v.rule == "language"
        for v in check_name("Die Straße nach Norden", language="eng").violations
    )
    assert check_name("Die Straße nach Norden", language="deu").ok


def test_a_blank_name_is_never_a_fault() -> None:
    check = check_name("")
    assert check.ok
    assert check.rule_fired == "blank"


def test_every_fault_is_reported_not_the_first() -> None:
    check = check_name("Chapter 3 at 00:14:02,", strict=True)
    assert len(check.violations) >= 3


# ----------------------------------------------------------- the blank answers
def test_the_last_chapter_with_nothing_said_is_the_closing() -> None:
    assert blank_for(11, 12) == "End Credits"
    assert blank_for(11, 12, language="deu") == "Abspann"


def test_a_short_first_chapter_with_nothing_said_is_the_opening() -> None:
    assert blank_for(0, 12, duration_s=48.0) == "Main Titles"
    assert blank_for(0, 12, duration_s=48.0, language="deu") == "Vorspann"


def test_a_long_first_chapter_is_not_a_title_sequence() -> None:
    assert blank_for(0, 12, duration_s=400.0) == ""


def test_a_silent_chapter_in_the_middle_gets_nothing() -> None:
    """Never invent a name from nothing."""
    assert blank_for(5, 12, duration_s=300.0) == ""


# ------------------------------------------------------------ names and windows
def test_a_name_invented_for_a_silent_window_is_blocked() -> None:
    texts = list(WINDOWS_TEXT)
    texts[3] = ""
    checks = check_names(list(NAMES), windows(texts))
    assert checks[3].blocked
    assert any(v.rule == "invented" for v in checks[3].violations)


def test_the_structural_answer_on_a_silent_window_fires_a_rule() -> None:
    texts = list(WINDOWS_TEXT)
    texts[5] = ""
    names = [*NAMES[:5], "End Credits"]
    checks = check_names(names, windows(texts))
    assert checks[5].ok
    assert checks[5].rule_fired == "no dialogue"


def test_a_blank_on_a_talking_window_costs_nothing() -> None:
    names = [*NAMES[:2], "", *NAMES[3:]]
    checks = check_names(names, windows())
    assert checks[2].ok
    assert checks[2].rule_fired == "left blank"


def test_a_name_list_of_the_wrong_length_is_an_error() -> None:
    try:
        check_names(list(NAMES[:4]), windows())
    except ValueError as exc:
        assert "different cut" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a shorter name list must not be accepted")


# ------------------------------------------------------------------ the matcher
def test_names_go_onto_the_files_own_marks() -> None:
    mine = marks()
    candidate = marks(NAMES, offset=0.8)
    result = match_names(mine, candidate)
    assert result.accepted
    assert result.chapters is not None
    assert result.chapters.names == NAMES
    assert result.chapters.starts_ns == mine.starts_ns


def test_grids_that_disagree_are_refused() -> None:
    other = ChapterSet(
        tuple(Chapter(round((i * 331.0) * SECOND), NAMES[i]) for i in range(6))
    )
    result = match_names(marks(), other)
    assert not result.accepted
    assert "same cut" in result.reason


def test_a_file_with_no_marks_is_refused_by_this_route() -> None:
    result = match_names(ChapterSet(), marks(NAMES))
    assert not result.accepted
    assert "no marks of its own" in result.reason


def test_a_candidate_of_labels_is_refused() -> None:
    labels = tuple(f"Chapter {i + 1}" for i in range(6))
    result = match_names(marks(), marks(labels))
    assert not result.accepted
    assert "not a label" in result.reason


def test_evidence_that_supports_the_names_lets_them_through() -> None:
    result = match_names(marks(), marks(NAMES), evidence=evidence())
    assert result.accepted
    assert result.verdict is not None and result.verdict.writable
    assert result.shifts is not None and result.shifts.best_shift == 0


def test_a_list_that_fits_better_elsewhere_is_refused_never_slid() -> None:
    """Shifting and writing is the tempting thing and it is wrong twice over."""
    slid = (*NAMES[2:], *NAMES[:2])
    result = match_names(marks(), marks(slid), evidence=evidence(names=slid))
    assert not result.accepted
    assert result.chapters is None
    assert result.shifts is not None and result.shifts.best_shift == 2
    assert "MISALIGNED" in result.reason


def test_a_label_among_the_names_arrives_blank_rather_than_as_a_name() -> None:
    """The mark then keeps the label its player shows, which is the truth."""
    mixed = (*NAMES[:5], "00:14:02")
    result = match_names(marks(), marks(mixed))
    assert result.accepted
    assert result.chapters is not None
    assert result.chapters.names[5] is None
    assert result.chapters.named_count == 5


def test_a_blocked_name_refuses_the_whole_film() -> None:
    damaged = (*NAMES[:5], "Rain on the Quarry� Road")
    result = match_names(marks(), marks(damaged))
    assert not result.accepted
    assert len(result.blocked_names) == 1


def test_a_name_that_is_only_untidy_does_not_refuse_the_film() -> None:
    untidy = (*NAMES[:5], "A Very Long Name Indeed That Runs Well Past The Limit")
    result = match_names(marks(), marks(untidy))
    assert result.accepted
    assert any(not check.ok for check in result.checks)
    assert "length" in str(result)


def test_the_rules_can_be_relaxed_by_the_caller() -> None:
    untidy = (*NAMES[:5], "A Very Long Name Indeed That Runs Well Past The Limit")
    relaxed = NamingRules(max_length=80)
    result = match_names(marks(), marks(untidy), rules=relaxed)
    assert all(check.ok for check in result.checks)


# ---------------------------------------------------------------- provenance
def test_the_tag_says_where_the_names_came_from() -> None:
    tag = provenance("an example chapter archive", when=dt.date(2026, 9, 23))
    names = {simple.name: simple.value for simple in tag.simples}
    assert names["CHAPTER_NAMES_SOURCE"] == "an example chapter archive"
    assert names["CHAPTER_NAMES_SOURCE_DATE"] == "2026-09-23"


def test_the_default_rules_are_the_published_ones() -> None:
    assert NAMING_RULES.max_length == 40
    assert NAMING_RULES.no_timecode
