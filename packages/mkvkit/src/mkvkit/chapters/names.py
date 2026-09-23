"""mkvkit.chapters.names -- putting a name list onto your own marks, or refusing.

Two things that belong together: the **matcher**, which decides whether a
name list may be copied onto the marks a file already has, and the **rules**,
which decide whether an individual name is one at all.

The matcher is short because every hard part of it is somewhere else.
:mod:`mkvkit.chapters.grid` says whether the two grids describe the same cut;
:mod:`mkvkit.chapters.verify` says whether the names were typed against those
marks. What is left is the sequence, and the sequence is the point: a list is
copied only when the grid agrees *and* nothing in the evidence says the names
belong elsewhere, and the times written are always the file's own.

**The list is never slid into place.** When the evidence says the names fit
better one mark along, that is reported and the whole film is refused. It is
tempting to shift and write, and it is wrong twice over: the offset that
scores best is a measurement with no error bar, and a list that needs
shifting is a list somebody typed against a different set of marks -- which
means nothing guarantees the rest of it is a permutation of this one rather
than a different list entirely.

The rules are the other half, and they apply to a name whoever wrote it. They
come in two strengths. A few are **blocking**: a "name" that is a timecode, a
bare label, or carries a byte a decoder could not read is not a name, and
lists like that do get written into files by a pass whose only test is that
the string is not empty. The rest are **advisory**: length, trailing punctuation,
quotation marks around the whole thing, a chapter number inside the name, and
whether the name is written in the language it is supposed to be written in.
A disc author breaks those routinely and their names are still worth having;
a program that has just written the names should not break them at all, which
is what ``strict`` is for.

**Blank is always allowed and always safe.** A window with nothing said in it
gets the structural name where one belongs and the empty string everywhere
else (:func:`blank_for`), and the mark then keeps the label its player shows.
Inventing a name from nothing is the one failure this whole area exists to
prevent.

Finally, a file that has been given names should be able to say so without
reference to a log somebody will delete: :func:`provenance` writes the tag
that records where the names came from.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..tags import Tag, Targets
from ..tags import provenance as _tag_provenance
from .grid import MATCH, GridMatch, copy_names, grid_match
from .verify import (
    DEFAULT_RUBRIC,
    FilmVerdict,
    MarkEvidence,
    Rubric,
    ShiftScores,
    shift_score,
)
from .verify import (
    verdict as film_verdict,
)
from .windows import Window, WindowSet
from .xml import REPLACEMENT_CHARACTER, ChapterSet, is_generic_name

__all__ = [
    "MAX_NAME_LENGTH",
    "NAMING_RULES",
    "OPENING_SECONDS",
    "PROVENANCE_KIND",
    "STRUCTURAL",
    "NameCheck",
    "NameMatch",
    "NamingRules",
    "RuleViolation",
    "blank_for",
    "check_name",
    "check_names",
    "match_names",
    "provenance",
]

log = logging.getLogger(__name__)

#: A chapter name is shown in a list in a player. Past this it is a sentence.
MAX_NAME_LENGTH: Final = 40

#: What a chapter with no dialogue is called, where a name belongs at all.
#: The first entry is the opening, the second the closing.
STRUCTURAL: Final[dict[str, tuple[str, str]]] = {
    "eng": ("Main Titles", "End Credits"),
    "deu": ("Vorspann", "Abspann"),
    "ger": ("Vorspann", "Abspann"),
    "fra": ("Générique de début", "Générique de fin"),
    "fre": ("Générique de début", "Générique de fin"),
}
#: A first chapter longer than this is not a title sequence, whatever it holds.
OPENING_SECONDS: Final = 90.0

#: What a provenance tag records here.
PROVENANCE_KIND: Final = "chapter names"

_TRAILING = tuple(".,;:!?-")
_LABEL_WORD = re.compile(
    r"(?i)\b(?:chapters?|kapitel|kapitola|chapitre|capitolo|cap[ií]tulo|hoofdstuk"
    r"|szene|scene)\b"
)
_TIMECODE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
#: Letters that occur in German and not in English.
_GERMAN_LETTERS = re.compile(r"[äöüßÄÖÜ]")


@dataclass(frozen=True)
class NamingRules:
    """The rules a name is held to. Every one of them is a policy."""

    max_length: int = MAX_NAME_LENGTH
    no_trailing_punctuation: bool = True
    no_wrapping_quotes: bool = True
    no_chapter_number: bool = True
    no_timecode: bool = True
    check_script: bool = True


NAMING_RULES: Final = NamingRules()


@dataclass(frozen=True)
class RuleViolation:
    """One rule a name breaks.

    ``blocking`` separates "this is not a name" from "this is a name somebody
    wrote carelessly". Only the first refuses a write on its own.
    """

    rule: str
    message: str
    blocking: bool = False

    def __str__(self) -> str:
        return f"{'reject' if self.blocking else 'note'}: {self.rule}: {self.message}"


@dataclass(frozen=True)
class NameCheck:
    """What the rules say about one name."""

    index: int
    name: str
    violations: tuple[RuleViolation, ...] = ()
    rule_fired: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def blocked(self) -> bool:
        return any(v.blocking for v in self.violations)

    def __str__(self) -> str:
        if self.ok:
            return f"{self.index:>3}  {self.name or '(blank)'}"
        problems = "; ".join(str(v) for v in self.violations)
        return f"{self.index:>3}  {self.name or '(blank)'}  -- {problems}"


# --------------------------------------------------------------------- the rules
def blank_for(
    position: int, count: int, *, duration_s: float = 0.0, language: str = "eng"
) -> str:
    """What a chapter with no dialogue is called. Mechanical, never judged.

    The last chapter is the closing credits; a short first chapter is the
    title sequence; everywhere else the answer is the empty string and the
    mark keeps the label a player shows for an unnamed chapter. Leaving this
    to judgement is how a silent chapter in the middle of a film acquires a
    name nobody could have known.
    """
    opening, closing = STRUCTURAL.get(language, STRUCTURAL["eng"])
    if count and position == count - 1:
        return closing
    if position == 0 and (duration_s <= 0.0 or duration_s <= OPENING_SECONDS):
        return opening
    return ""


def check_name(
    name: str | None,
    *,
    index: int = 0,
    rules: NamingRules = NAMING_RULES,
    language: str = "eng",
    strict: bool = False,
) -> NameCheck:
    """Everything the rules say about one name, in one pass.

    Every problem is reported, not the first one: a name with three faults
    should be rewritten once.
    """
    text = (name or "").strip()
    if not text:
        return NameCheck(index, "", (), rule_fired="blank")
    violations: list[RuleViolation] = []

    def add(rule: str, message: str, *, blocking: bool = False) -> None:
        violations.append(RuleViolation(rule, message, blocking or strict))

    if REPLACEMENT_CHARACTER in text:
        add("undecodable", "the name carries a byte that could not be decoded",
            blocking=True)
    if is_generic_name(text):
        add("label", f"{text!r} is a label, not a name", blocking=True)
    if rules.no_timecode and _TIMECODE.search(text):
        add("timecode", "a name never carries a time; the marks carry the times",
            blocking=True)
    if rules.no_chapter_number and _LABEL_WORD.search(text):
        add("chapter number", "the word for a chapter belongs in the label, not "
            "in the name")
    if len(text) > rules.max_length:
        add("length", f"{len(text)} characters, over {rules.max_length}")
    if (name or "") != text:
        add("whitespace", "the name has whitespace around it")
    if rules.no_trailing_punctuation and text.endswith(_TRAILING):
        add("trailing punctuation", f"the name ends in {text[-1]!r}")
    if rules.no_wrapping_quotes and len(text) > 1 and (
        (text[0], text[-1]) in {('"', '"'), ("'", "'"), ("“", "”")}
    ):
        add("quotes", "the whole name is in quotation marks")
    if rules.check_script:
        problem = _script_problem(text, language)
        if problem:
            add("language", problem)
    return NameCheck(index, text, tuple(violations))


def _script_problem(text: str, language: str) -> str | None:
    """Whether a name is plainly not in the language it should be in.

    Deliberately weak. Telling two languages apart from four words is not
    reliably possible, and a false accusation here holds back a film whose
    names are fine. Only two things are checked: a letter that belongs to one
    language turning up in a name that should be in another, and a script the
    target language does not use at all.
    """
    if language in {"eng"} and _GERMAN_LETTERS.search(text):
        return "the name uses letters the target language does not have"
    scripts = {
        "LATIN" if "LATIN" in unicodedata.name(ch, "") else ""
        for ch in text
        if ch.isalpha()
    }
    if language in {"eng", "deu", "ger", "fra", "fre"} and scripts - {"LATIN"}:
        return "the name is not written in the script the target language uses"
    return None


def check_names(
    names: Sequence[str | None],
    windows: WindowSet | Sequence[Window] | None = None,
    *,
    rules: NamingRules = NAMING_RULES,
    language: str | None = None,
    strict: bool = False,
) -> list[NameCheck]:
    """The rules over a whole name list, with the windows where there are any.

    Passing the windows turns on the one check that needs them: a name where
    nothing is said has to be the mechanical answer for that position, and
    anything else was invented rather than read. A blank name on a window that
    *does* have dialogue costs nothing and is recorded as a fired rule, never
    as a fault -- a missing name is always safe.
    """
    if isinstance(windows, WindowSet):
        target_language = language or windows.names_language
        window_list: Sequence[Window] | None = windows.windows
    else:
        target_language = language or "eng"
        window_list = windows
    if window_list is not None and len(window_list) != len(names):
        raise ValueError(
            f"{len(names)} name(s) for {len(window_list)} window(s): a name list of "
            "a different length describes a different cut"
        )

    out: list[NameCheck] = []
    for position, name in enumerate(names):
        window = window_list[position] if window_list is not None else None
        check = check_name(
            name, index=position + 1, rules=rules,
            language=target_language, strict=strict,
        )
        if window is not None and not window.has_dialogue:
            expected = blank_for(
                position, len(names),
                duration_s=window.duration_s, language=target_language,
            )
            text = (name or "").strip()
            if text == expected:
                check = NameCheck(check.index, text, (), rule_fired="no dialogue")
            elif text:
                check = NameCheck(
                    check.index, text,
                    (
                        *check.violations,
                        RuleViolation(
                            "invented",
                            f"nothing is said in this window; the answer here is "
                            f"{expected!r}",
                            blocking=True,
                        ),
                    ),
                )
        elif not (name or "").strip():
            check = NameCheck(check.index, "", (), rule_fired="left blank")
        out.append(check)
    return out


# ------------------------------------------------------------------- the matcher
@dataclass(frozen=True)
class NameMatch:
    """Whether a name list may be written, and the marks that would carry it."""

    accepted: bool
    reason: str
    chapters: ChapterSet | None = None
    grid: GridMatch | None = None
    shifts: ShiftScores | None = None
    verdict: FilmVerdict | None = None
    checks: tuple[NameCheck, ...] = ()

    @property
    def blocked_names(self) -> tuple[NameCheck, ...]:
        return tuple(check for check in self.checks if check.blocked)

    def __str__(self) -> str:
        head = f"{'accepted' if self.accepted else 'refused'}: {self.reason}"
        notes = [str(check) for check in self.checks if not check.ok]
        return "\n".join([head, *(f"  {note}" for note in notes)])


def match_names(
    marks: ChapterSet,
    candidate: ChapterSet,
    *,
    evidence: Sequence[MarkEvidence] | None = None,
    rubric: Rubric = DEFAULT_RUBRIC,
    rules: NamingRules = NAMING_RULES,
    language: str = "eng",
) -> NameMatch:
    """Copy a candidate's names onto the file's own marks, or say why not.

    Five gates, in the order that makes the cheapest refusal first: the grids
    have to describe the same cut, the candidate has to carry names rather
    than labels, the name list has to survive the blocking rules, and -- where
    evidence was collected -- the evidence has to say the names sit on the
    marks they describe.

    The times in the result are always the file's own. That is what makes this
    the safe operation: the worst outcome that survives every gate is a wrong
    name on a mark that has not moved.
    """
    if not len(marks):
        return NameMatch(
            False,
            "the file has no marks of its own; copying names needs marks to copy "
            "them onto",
        )
    grid = grid_match(marks, candidate)
    if grid.verdict != MATCH:
        return NameMatch(
            False, f"the grids do not describe the same cut ({grid})", grid=grid
        )
    if not candidate.named_count:
        return NameMatch(
            False,
            "the candidate carries no name that is not a label",
            grid=grid,
        )

    shifts: ShiftScores | None = None
    outcome: FilmVerdict | None = None
    if evidence is not None:
        shifts = shift_score(
            list(candidate.names), [e.transcript for e in evidence]
        )
        outcome = film_verdict(evidence, shifts, rubric=rubric)
        if not outcome.writable:
            return NameMatch(
                False,
                f"the evidence does not support these names: {outcome}",
                grid=grid, shifts=shifts, verdict=outcome,
            )

    renamed = copy_names(marks, candidate, grid)
    checks = tuple(
        check_names(list(renamed.names), rules=rules, language=language)
    )
    blocked = [check for check in checks if check.blocked]
    if blocked:
        return NameMatch(
            False,
            f"{len(blocked)} of {len(checks)} entries are not names",
            chapters=None, grid=grid, shifts=shifts, verdict=outcome, checks=checks,
        )
    return NameMatch(
        True,
        f"{renamed.named_count} name(s) onto the file's own {len(marks)} mark(s)",
        chapters=renamed, grid=grid, shifts=shifts, verdict=outcome, checks=checks,
    )


# ---------------------------------------------------------------------- the tag
def provenance(
    source: str,
    *,
    when: dt.date | None = None,
    tool: str | None = None,
) -> Tag:
    """The tag that records where a file's chapter names came from.

    Worth insisting on. Names written by a program and names typed by whoever
    authored the disc look identical in a player and are worth very different
    amounts, and the only moment the difference is known is the moment they
    are written.
    """
    return _tag_provenance(PROVENANCE_KIND, source, when=when, tool=tool,
                           targets=Targets())
