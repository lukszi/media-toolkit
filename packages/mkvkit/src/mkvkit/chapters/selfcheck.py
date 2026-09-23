"""mkvkit.chapters.selfcheck -- grade the names before anything is written.

A second pass over names that have just been written, against the windows
they were written from. It exists because the first pass is confident and the
failures are quiet: a name that describes the wrong part of its own chapter
looks exactly like a name that describes the right part.

Four grades, and the fourth is the interesting one.

``good``
    Something the name says is said near the start of its own window. A
    viewer scrubbing to that mark would recognise it.
``summary-ish``
    Accurate as far as anything here can tell, and not much use: vague, or
    supported only by the end of the window. Usable, not great.
``wrong``
    Breaks a rule, repeats another chapter's name, sits out of order, or
    describes a scene that starts at a different mark.
``rule-fired``
    A correct mechanical result -- the structural name where one belongs, or
    a blank. A blank is never a fault. A missing name costs nothing and the
    mark keeps the label its player shows.

**Any wrong grade holds the whole film back.** Not the chapter: the film. The
names go into a file's header together, a pass over a collection is judged by
whether anybody has to go back through it, and a film left alone costs
nothing but the names it did not get. That asymmetry is the whole reason this
pass is worth running.

**What is mechanical, and what this cannot see.** The two failure modes the
original trial found were *the climax reach* -- a window that opens quietly
and ends dramatically, named for the ending -- and *outside knowledge*, a
character or place that never appears in the window text. The first has a
mechanical shadow and is caught here: where a name's words appear only in the
last third of its window, the name describes the end rather than the
beginning. The second does not, and is not claimed: a name supported by
nothing in its window is graded ``summary-ish`` with the reason given, and a
reader can look. Grades from a reader can be passed in and are summarised by
the same rules, so the bar is applied in one place whoever applied the grades.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .names import NAMING_RULES, NamingRules, blank_for, check_names
from .verify import better_elsewhere, content_words, hit_rate
from .windows import Window, WindowSet

__all__ = [
    "CLEAN",
    "FLAGGED",
    "GOOD",
    "MIN_COVERAGE",
    "OPENING_FRACTION",
    "RULE_FIRED",
    "SUMMARY_ISH",
    "WRONG",
    "Grade",
    "SelfCheck",
    "grade_names",
    "summarise",
]

log = logging.getLogger(__name__)

GOOD: Final = "good"
SUMMARY_ISH: Final = "summary-ish"
WRONG: Final = "wrong"
RULE_FIRED: Final = "rule-fired"

#: A film with no wrong grade is clean; one wrong grade and it is held back.
CLEAN: Final = "clean"
FLAGGED: Final = "flagged"

#: Below this share of dialogue-bearing windows named, the pass did not do its
#: job even if every name it did write is fine.
MIN_COVERAGE: Final = 0.60
#: Where a name's only support sits past this point in its own window, the
#: name describes the end of the chapter rather than its beginning.
OPENING_FRACTION: Final = 0.66

#: Words that put a name in a sequence. Out of sequence they are a fault, and
#: a mechanical one: the order is in the list, not in the film.
_ORDER_WORDS: Final[dict[str, int]] = {
    "prologue": -2, "main titles": -2, "opening titles": -2, "vorspann": -2,
    "part one": -1, "part two": 1, "part three": 1, "continued": 1,
    "finale": 2, "epilogue": 2, "end credits": 2, "abspann": 2,
    "closing credits": 2,
}
_WORD = re.compile(r"[^0-9A-Za-zÀ-ɏ']+")


@dataclass(frozen=True)
class Grade:
    """One name's grade, and why."""

    index: int
    name: str
    grade: str
    note: str = ""

    @property
    def holds_back(self) -> bool:
        return self.grade == WRONG

    def __str__(self) -> str:
        suffix = f"  -- {self.note}" if self.note else ""
        return f"{self.index:>3}  {self.grade:<12} {self.name or '(blank)'}{suffix}"


@dataclass(frozen=True)
class SelfCheck:
    """Every grade, the counts, and whether the film may be written."""

    grades: tuple[Grade, ...] = ()
    verdict: str = CLEAN
    reason: str = ""
    coverage: float = 0.0
    named: int = 0
    checkable: int = 0

    @property
    def clean(self) -> bool:
        return self.verdict == CLEAN

    @property
    def counts(self) -> dict[str, int]:
        out = {GOOD: 0, SUMMARY_ISH: 0, WRONG: 0, RULE_FIRED: 0}
        for grade in self.grades:
            out[grade.grade] += 1
        return out

    @property
    def wrong(self) -> tuple[Grade, ...]:
        return tuple(g for g in self.grades if g.grade == WRONG)

    def __str__(self) -> str:
        counts = self.counts
        head = (
            f"{self.verdict}: {self.reason} "
            f"({counts[GOOD]} good, {counts[SUMMARY_ISH]} summary-ish, "
            f"{counts[WRONG]} wrong, {counts[RULE_FIRED]} rule-fired)"
        )
        return "\n".join([head, *(f"  {g}" for g in self.grades if g.grade != GOOD)])


def grade_names(
    names: Sequence[str | None],
    windows: WindowSet | Sequence[Window],
    *,
    rules: NamingRules = NAMING_RULES,
    language: str | None = None,
) -> SelfCheck:
    """Grade every name against its own window, then summarise.

    The rule checks are run in their strict form, because these names were
    written by this pass a moment ago: a name over the length, ending in a
    full stop or carrying a chapter number is a fault here even though the
    same name off a disc would only be a note.
    """
    window_list = list(windows.windows if isinstance(windows, WindowSet) else windows)
    target = language or (
        windows.names_language if isinstance(windows, WindowSet) else "eng"
    )
    if len(window_list) != len(names):
        raise ValueError(
            f"{len(names)} name(s) for {len(window_list)} window(s): a name list of "
            "a different length describes a different cut"
        )
    checks = check_names(
        names, window_list, rules=rules, language=target, strict=True
    )
    texts = [w.text if w.has_dialogue else "" for w in window_list]
    seen: dict[str, int] = {}
    grades: list[Grade] = []

    for position, (name, window, check) in enumerate(
        zip(names, window_list, checks, strict=True)
    ):
        text = (name or "").strip()
        index = position + 1
        if not text:
            expected = blank_for(
                position, len(names),
                duration_s=window.duration_s, language=target,
            )
            note = (
                "left blank; a missing name costs nothing"
                if window.has_dialogue
                else (
                    f"nothing is said here and the answer is {expected!r}"
                    if expected
                    else "nothing is said here, so there is nothing to call it"
                )
            )
            if not window.has_dialogue and expected:
                grades.append(Grade(index, "", WRONG,
                                    f"the structural name {expected!r} belongs here"))
            else:
                grades.append(Grade(index, "", RULE_FIRED, note))
            continue
        if check.blocked:
            grades.append(
                Grade(index, text, WRONG,
                      "; ".join(v.message for v in check.violations if v.blocking))
            )
            continue
        if check.violations:
            grades.append(
                Grade(index, text, WRONG,
                      "; ".join(v.message for v in check.violations))
            )
            continue
        if check.rule_fired == "no dialogue":
            grades.append(Grade(index, text, RULE_FIRED,
                                "the structural name where one belongs"))
            continue

        key = _key(text)
        if key in seen:
            grades.append(
                Grade(index, text, WRONG,
                      f"the same name is already on mark {seen[key]}")
            )
            continue
        seen[key] = index

        misplaced = _order_problem(text, position, len(names))
        if misplaced:
            grades.append(Grade(index, text, WRONG, misplaced))
            continue

        grades.append(_content_grade(index, text, window, texts, position))

    return summarise(grades, window_list)


def _key(name: str) -> str:
    return " ".join(_WORD.sub(" ", name.casefold()).split())


def _order_problem(name: str, position: int, count: int) -> str:
    """Whether a name that carries an order sits somewhere it cannot."""
    where = _ORDER_WORDS.get(_key(name))
    if where is None:
        return ""
    first_third = position < max(1, count // 3)
    last_third = position >= count - max(1, count // 3)
    if where <= -1 and not first_third:
        return "an opening name away from the opening"
    if where >= 1 and not last_third:
        return "a closing name away from the end"
    return ""


def _content_grade(
    index: int, name: str, window: Window, texts: Sequence[str], position: int
) -> Grade:
    """Grade a well-formed name by where its words turn up."""
    if not window.has_dialogue:
        return Grade(index, name, RULE_FIRED, "nothing is said in this window")
    own = hit_rate(name, window.text)
    if own is None:
        return Grade(index, name, SUMMARY_ISH,
                     "the name carries no word that could be looked for")
    if own[0] == 0:
        elsewhere = better_elsewhere(name, texts, position)
        if elsewhere is not None:
            return Grade(
                index, name, WRONG,
                f"nothing from the name is said here; it fits mark "
                f"{elsewhere[0] + 1} instead",
            )
        return Grade(index, name, SUMMARY_ISH,
                     "nothing in the window supports the name, and nothing "
                     "contradicts it")
    where = _first_hit_fraction(name, window.text)
    if where is not None and where > OPENING_FRACTION:
        return Grade(
            index, name, SUMMARY_ISH,
            "the name is only supported by the end of the window; a chapter is "
            "named for what it opens with",
        )
    return Grade(index, name, GOOD,
                 f"{own[0]} of {own[1]} word(s) from the name are said here")


def _first_hit_fraction(name: str, text: str) -> float | None:
    """How far into the window the name's first supporting word appears."""
    words = text.split()
    if not words:
        return None
    wanted = set(content_words(name))
    if not wanted:
        return None
    for position, word in enumerate(words):
        cleaned = _WORD.sub("", word.casefold())
        if cleaned and any(cleaned.startswith(w[:4]) for w in wanted):
            return position / len(words)
    return None


def summarise(grades: Sequence[Grade], windows: Sequence[Window]) -> SelfCheck:
    """Apply the bar to a set of grades, whoever produced them.

    Separate from the grading on purpose. A pass whose rubric was sharpened
    halfway through can be re-summarised over the grades it already has, and
    grades from a person go through exactly the same arithmetic as grades
    from this module.
    """
    with_dialogue = [w for w in windows if w.has_dialogue]
    written = [
        g for g in grades
        if g.name and any(w.index == g.index and w.has_dialogue for w in windows)
    ]
    coverage = len(written) / len(with_dialogue) if with_dialogue else 1.0
    named = sum(1 for g in grades if g.name)
    checkable = sum(1 for g in grades if g.grade in {GOOD, SUMMARY_ISH, WRONG})
    wrong = [g for g in grades if g.grade == WRONG]
    if wrong:
        return SelfCheck(
            tuple(grades), FLAGGED,
            f"{len(wrong)} name(s) graded wrong; the film is held back",
            coverage, named, checkable,
        )
    if coverage < MIN_COVERAGE:
        return SelfCheck(
            tuple(grades), FLAGGED,
            f"only {coverage:.0%} of the windows with dialogue were named, under "
            f"{MIN_COVERAGE:.0%}",
            coverage, named, checkable,
        )
    return SelfCheck(
        tuple(grades), CLEAN,
        f"nothing graded wrong, {coverage:.0%} of the windows with dialogue named",
        coverage, named, checkable,
    )
