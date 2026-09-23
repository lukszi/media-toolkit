"""Grading written names, and the rule that one wrong grade holds the film back.

The test that carries the most weight is the climax reach: a window that
opens quietly and ends dramatically, named for the ending. It was one of the
two failure modes the original trial found, and it is the one with a
mechanical shadow.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import pytest
from mkvkit.chapters.selfcheck import (
    CLEAN,
    FLAGGED,
    GOOD,
    RULE_FIRED,
    SUMMARY_ISH,
    WRONG,
    Grade,
    grade_names,
    summarise,
)
from mkvkit.chapters.windows import NO_DIALOGUE, Window

TEXTS = (
    "the harbour is quiet at dawn and the boats have not gone out",
    "a letter came up from the coast this morning addressed to nobody",
    "we drive north all night and the road never seems to end",
    "rain on the quarry road turns the whole hillside into mud",
    "the foreman knew about the pumps and said nothing to anybody",
    "the last ferry leaves at six and we are not going to be on it",
)
NAMES = (
    "The Harbour at Dawn",
    "A Letter from the Coast",
    "The Long Drive North",
    "Rain on the Quarry Road",
    "What the Foreman Knew",
    "The Last Ferry",
)


def windows(texts=TEXTS, duration_s: float = 300.0) -> list[Window]:
    return [
        Window(
            index=i + 1, start_s=i * duration_s, end_s=(i + 1) * duration_s,
            text=texts[i] or NO_DIALOGUE, cues=4 if texts[i] else 0,
        )
        for i in range(len(texts))
    ]


# --------------------------------------------------------------------- grades
def test_names_that_describe_their_own_window_are_good() -> None:
    checked = grade_names(list(NAMES), windows())
    assert checked.counts[GOOD] == 6
    assert checked.verdict == CLEAN
    assert checked.clean


def test_the_climax_reach_is_caught() -> None:
    """A quiet opening and a dramatic ending, named for the ending."""
    texts = list(TEXTS)
    texts[0] = (
        "nothing much is happening here and nobody is saying anything worth "
        "repeating while the morning goes by slowly and the kettle boils and "
        "somebody reads the paper and then at the very end of all of it the "
        "lantern falls"
    )
    names = list(NAMES)
    names[0] = "The Falling Lantern"
    checked = grade_names(names, windows(texts))
    assert checked.grades[0].grade == SUMMARY_ISH
    assert "opens with" in checked.grades[0].note


def test_a_name_describing_another_marks_scene_is_wrong() -> None:
    names = list(NAMES)
    names[1] = "The Foreman and the Pumps"
    checked = grade_names(names, windows())
    assert checked.grades[1].grade == WRONG
    assert "mark 5" in checked.grades[1].note
    assert checked.verdict == FLAGGED


def test_a_name_nothing_supports_is_summary_ish_not_wrong() -> None:
    """Outside knowledge has no mechanical shadow and is not claimed."""
    names = list(NAMES)
    names[2] = "Quiet Bastion"
    checked = grade_names(names, windows())
    assert checked.grades[2].grade == SUMMARY_ISH


def test_a_repeated_name_is_wrong() -> None:
    names = list(NAMES)
    names[4] = NAMES[0]
    checked = grade_names(names, windows())
    assert checked.grades[4].grade == WRONG
    assert "already on mark 1" in checked.grades[4].note


def test_a_closing_name_away_from_the_end_is_wrong() -> None:
    names = list(NAMES)
    names[1] = "End Credits"
    checked = grade_names(names, windows())
    assert checked.grades[1].grade == WRONG
    assert "away from the end" in checked.grades[1].note


def test_an_opening_name_away_from_the_opening_is_wrong() -> None:
    names = list(NAMES)
    names[4] = "Main Titles"
    assert grade_names(names, windows()).grades[4].grade == WRONG


def test_a_structural_name_where_it_belongs_fires_a_rule() -> None:
    texts = list(TEXTS)
    texts[5] = ""
    names = [*NAMES[:5], "End Credits"]
    checked = grade_names(names, windows(texts))
    assert checked.grades[5].grade == RULE_FIRED
    assert checked.verdict == CLEAN


def test_a_blank_on_a_talking_window_is_a_fired_rule_not_a_fault() -> None:
    names = [*NAMES[:3], "", *NAMES[4:]]
    checked = grade_names(names, windows())
    assert checked.grades[3].grade == RULE_FIRED
    assert checked.verdict == CLEAN


def test_a_silent_window_that_should_have_had_the_structural_name_is_wrong() -> None:
    texts = list(TEXTS)
    texts[5] = ""
    names = [*NAMES[:5], ""]
    checked = grade_names(names, windows(texts))
    assert checked.grades[5].grade == WRONG


def test_a_name_invented_for_a_silent_window_is_wrong() -> None:
    texts = list(TEXTS)
    texts[3] = ""
    checked = grade_names(list(NAMES), windows(texts))
    assert checked.grades[3].grade == WRONG


# ----------------------------------------------------- the mechanical rules
@pytest.mark.parametrize(
    "bad",
    [
        "A Very Long Name Indeed That Runs Well Past The Limit",
        "The Harbour at Dawn.",
        "Chapter 4",
        "Arrival at 12:40",
    ],
)
def test_a_rule_break_is_wrong_for_a_name_this_pass_wrote(bad: str) -> None:
    names = list(NAMES)
    names[2] = bad
    checked = grade_names(names, windows())
    assert checked.grades[2].grade == WRONG
    assert checked.verdict == FLAGGED


def test_a_name_in_the_wrong_language_is_wrong() -> None:
    names = list(NAMES)
    names[2] = "Die Straße nach Norden"
    assert grade_names(names, windows(), language="eng").grades[2].grade == WRONG


# ------------------------------------------------------------------ the bar
def test_one_wrong_grade_holds_the_whole_film_back() -> None:
    """Not the chapter: the film. A film left alone costs nothing."""
    names = list(NAMES)
    names[1] = "The Foreman and the Pumps"
    checked = grade_names(names, windows())
    assert checked.verdict == FLAGGED
    assert not checked.clean
    assert len(checked.wrong) == 1
    assert "held back" in checked.reason


def test_thin_coverage_flags_a_film_whose_names_are_all_fine() -> None:
    names = [NAMES[0], "", "", "", "", NAMES[5]]
    checked = grade_names(names, windows())
    assert checked.counts[WRONG] == 0
    assert checked.verdict == FLAGGED
    assert checked.coverage == pytest.approx(2 / 6)


def test_the_bar_is_applied_in_one_place_whoever_graded() -> None:
    """Grades from a reader go through exactly the same arithmetic."""
    by_hand = [
        Grade(1, NAMES[0], GOOD),
        Grade(2, NAMES[1], SUMMARY_ISH, "vague"),
        Grade(3, NAMES[2], WRONG, "names the wrong scene"),
        Grade(4, NAMES[3], GOOD),
        Grade(5, NAMES[4], GOOD),
        Grade(6, NAMES[5], GOOD),
    ]
    assert summarise(by_hand, windows()).verdict == FLAGGED


def test_a_name_list_of_the_wrong_length_is_an_error() -> None:
    with pytest.raises(ValueError, match="different cut"):
        grade_names(list(NAMES[:3]), windows())


def test_the_report_leads_with_what_is_not_good() -> None:
    names = list(NAMES)
    names[1] = "The Foreman and the Pumps"
    text = str(grade_names(names, windows()))
    assert text.splitlines()[0].startswith(FLAGGED)
    assert "The Foreman and the Pumps" in text
