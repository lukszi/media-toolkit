"""mkvkit.chapters.xml -- read a chapter document, write one, check it first.

A chapter document is small, easy to produce and easy to produce *wrongly*,
and a wrong one is expensive: applying it replaces the whole chapters element,
so a document with two marks where the file had sixteen does not add two
marks, it deletes fourteen. Everything in this module exists to make that
mistake loud before anything is written.

**What a correct document looks like.** One edition, marked default. Per mark:
a start time to nanosecond precision, the hidden flag off, the enabled flag
on, and a display block **only where there is a real name**.

**Leaving a mark unnamed is the way to say "unnamed".** Omitting the display
block is correct and a player labels the mark generically. Writing the
generic label into the file yourself is not the same thing: it looks
identical in a player and it is irreversible by inspection, because nothing
downstream can tell a name somebody chose from a label a program made up.
:func:`downstream_label` and :func:`is_generic_name` are how a caller checks
that, and :func:`selfcheck` rejects a document that is generic labels all the
way down.

**The chapter language is spelled the other way round.** The element wants the
bibliographic code while everything else in this package uses the
terminological one, so the writer converts at the boundary and the reader
converts back.

**Rollback is a document, not a copy of the file.** Where only the names
changed, undoing them means re-emitting the same start times with no display
block (:func:`rollback`). Generating it costs nothing and it is generated
every time; applying it is a decision somebody makes later.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Final

from ..config import Config
from ..langcodes import bibliographic, canonical
from ..run import Runner, default_runner

__all__ = [
    "Chapter",
    "ChapterError",
    "ChapterSet",
    "Problem",
    "build",
    "downstream_label",
    "format_timestamp",
    "is_generic_name",
    "parse",
    "parse_document",
    "parse_timestamp",
    "read_chapters",
    "rollback",
    "selfcheck",
    "trim_trailing",
]

#: A mark this early is normal; later than this is worth a note.
FIRST_MARK_S: Final = 5.0
#: A last mark closer than this to the end of the runtime is worth a note.
LAST_MARK_MARGIN_S: Final = 30.0
#: Two marks closer together than this are the same mark written twice.
SAME_MARK_NS: Final = 1_000_000

#: The word for a chapter, in the languages this shape has been seen in.
_LABEL_WORD = r"(?i:chapters?|kapitel|chapitre|capitolo|cap[ií]tulo|scenes?|szene|part|teil)"
#: A number spelled out. Only counted as a label after the word above: a mark
#: legitimately called "Seven" is a name, "Chapter Seven" is not.
_LABEL_NUMBER = (
    r"(?i:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|eins|zwei|drei|vier|f[uü]nf|sechs|sieben|acht|neun|zehn|elf|zw[oö]lf)"
)
#: The word, with or without a number after it.
_GENERIC_LABELLED = re.compile(
    rf"^\s*{_LABEL_WORD}\s*[.:#-]*\s*(?:[0-9]{{1,3}}|[IVXLCivxlc]{{1,6}}|{_LABEL_NUMBER})?"
    r"\s*[.:#-]*\s*$"
)
#: A bare number, or a bare upper-case roman numeral. The case matters: an
#: ordinary word made only of those letters ("Civil") is a name.
_GENERIC_BARE = re.compile(r"^\s*[#]?\s*(?:[0-9]{1,3}|[IVXLC]{1,6})\s*[.:-]?\s*$")
#: A "name" that is a timecode, which is what a badly exported list contains.
_TIMECODE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\s*$")
#: What a decoder writes where it could not decode a byte.
REPLACEMENT_CHARACTER: Final = "�"

_HEADER: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">\n'
)


class ChapterError(ValueError):
    """A document could not be read, or could not be read unambiguously."""


# ------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class Chapter:
    """One mark. The time is the mark; the name is a claim about it."""

    start_ns: int
    name: str | None = None
    end_ns: int | None = None
    language: str | None = None
    uid: int | None = None
    hidden: bool = False
    enabled: bool = True

    @property
    def start_s(self) -> float:
        return self.start_ns / 1e9

    @property
    def is_named(self) -> bool:
        """A name that says something. A generic label does not count."""
        return bool(self.name and not is_generic_name(self.name))


@dataclass(frozen=True)
class ChapterSet:
    """One edition: the marks, in the order the file carries them."""

    chapters: tuple[Chapter, ...] = ()
    edition_uid: int | None = None
    default: bool = True
    ordered: bool = False
    source: str | None = None

    def __len__(self) -> int:
        return len(self.chapters)

    def __iter__(self) -> Iterator[Chapter]:
        return iter(self.chapters)

    def __getitem__(self, index: int) -> Chapter:
        return self.chapters[index]

    @property
    def starts_ns(self) -> tuple[int, ...]:
        return tuple(c.start_ns for c in self.chapters)

    @property
    def starts_s(self) -> tuple[float, ...]:
        return tuple(c.start_s for c in self.chapters)

    @property
    def names(self) -> tuple[str | None, ...]:
        return tuple(c.name for c in self.chapters)

    @property
    def named_count(self) -> int:
        return sum(1 for c in self.chapters if c.is_named)

    def renamed(self, names: Sequence[str | None]) -> ChapterSet:
        """The same marks with a new name list. Times are never touched here.

        This is the only supported way to put somebody else's names on your
        own marks: the marks stay exactly where the file has them, so the
        worst case is a wrong name rather than a wrong cut.
        """
        if len(names) != len(self.chapters):
            raise ChapterError(
                f"{len(names)} names for {len(self.chapters)} marks: "
                "a name list of a different length describes a different cut"
            )
        return replace(
            self,
            chapters=tuple(
                replace(chapter, name=(name or None))
                for chapter, name in zip(self.chapters, names, strict=True)
            ),
        )

    def stripped(self) -> ChapterSet:
        """The same marks with no names at all: the rollback state."""
        return replace(
            self, chapters=tuple(replace(c, name=None) for c in self.chapters)
        )


@dataclass(frozen=True)
class Problem:
    """Something the self-check found. ``reject`` blocks; ``note`` does not."""

    level: str
    rule: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.level == "reject"

    def __str__(self) -> str:
        return f"{self.level}: {self.rule}: {self.message}"


# ---------------------------------------------------------------------- reading
def parse_timestamp(text: str | None) -> int | None:
    """``HH:MM:SS.nnnnnnnnn`` to nanoseconds, or ``None`` if it is not one."""
    if text is None:
        return None
    match = re.match(r"^\s*(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,9}))?\s*$", text)
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    total = (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1_000_000_000
    if fraction:
        total += int(fraction.ljust(9, "0"))
    return total


def format_timestamp(nanoseconds: int) -> str:
    """Nanoseconds to the spelling the element wants, without losing a digit."""
    if nanoseconds < 0:
        raise ChapterError(f"a mark cannot be at {nanoseconds} ns")
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{fraction:09d}"


def parse_document(text: str) -> tuple[ChapterSet, ...]:
    """Every edition in a document, in order."""
    try:
        root = ET.fromstring(text.lstrip("﻿"))
    except ET.ParseError as exc:
        raise ChapterError(f"the document does not parse: {exc}") from exc
    editions = root.findall("EditionEntry")
    if not editions and root.tag == "EditionEntry":
        editions = [root]
    return tuple(_edition(entry) for entry in editions)


def parse(text: str) -> ChapterSet:
    """The one edition in a document.

    More than one is an error rather than a choice: applying a document
    replaces every edition in the file, so a tool that silently picks the
    first would delete the others.
    """
    editions = parse_document(text)
    if not editions:
        return ChapterSet()
    if len(editions) > 1:
        raise ChapterError(
            f"{len(editions)} editions in one document; applying it would replace "
            "all of them, so which one is meant has to be decided by a person"
        )
    return editions[0]


def read_chapters(
    path: Path | str, *, runner: Runner | None = None, config: Config | None = None
) -> ChapterSet:
    """The marks a file carries. A file with none gives an empty set.

    Reading them back as a document -- rather than as the player's view of
    them -- is what makes a rollback possible: what comes out is what would
    have to go back in.
    """
    run = runner if runner is not None else default_runner(config)
    result = run("mkvextract", [str(path), "chapters"], ok=(0, 1))
    text = result.stdout.strip()
    return parse(text) if text else ChapterSet()


def _edition(entry: ET.Element) -> ChapterSet:
    chapters: list[Chapter] = []
    for atom in entry.findall("ChapterAtom"):
        start = parse_timestamp(atom.findtext("ChapterTimeStart"))
        if start is None:
            raise ChapterError("a mark has no readable start time")
        display = atom.find("ChapterDisplay")
        name: str | None = None
        language: str | None = None
        if display is not None:
            raw = display.findtext("ChapterString")
            name = raw if (raw or "").strip() else None
            language = canonical(display.findtext("ChapterLanguage"))
        chapters.append(
            Chapter(
                start_ns=start,
                name=name,
                end_ns=parse_timestamp(atom.findtext("ChapterTimeEnd")),
                language=language,
                uid=_int(atom.findtext("ChapterUID")),
                hidden=_flag(atom.findtext("ChapterFlagHidden"), False),
                enabled=_flag(atom.findtext("ChapterFlagEnabled"), True),
            )
        )
    return ChapterSet(
        chapters=tuple(chapters),
        edition_uid=_int(entry.findtext("EditionUID")),
        default=_flag(entry.findtext("EditionFlagDefault"), True),
        ordered=_flag(entry.findtext("EditionFlagOrdered"), False),
    )


def _int(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _flag(text: str | None, default: bool) -> bool:
    value = (text or "").strip()
    return default if not value else value not in {"0", "false", "no"}


# ---------------------------------------------------------------------- writing
def build(
    chapters: ChapterSet | Iterable[Chapter],
    *,
    language: str = "und",
    keep_uids: bool = False,
) -> str:
    """The document, ready to be written.

    ``language`` is the terminological code the names are in; it is converted
    to the spelling the element wants. Identifiers are left out unless a
    caller asks for them: they are regenerated on write anyway, and a document
    that carries none is one that cannot collide with the file's own.
    """
    chapter_set = (
        chapters if isinstance(chapters, ChapterSet) else ChapterSet(tuple(chapters))
    )
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")
    if keep_uids and chapter_set.edition_uid is not None:
        ET.SubElement(edition, "EditionUID").text = str(chapter_set.edition_uid)
    ET.SubElement(edition, "EditionFlagDefault").text = "1" if chapter_set.default else "0"
    if chapter_set.ordered:
        ET.SubElement(edition, "EditionFlagOrdered").text = "1"
    for chapter in chapter_set:
        atom = ET.SubElement(edition, "ChapterAtom")
        if keep_uids and chapter.uid is not None:
            ET.SubElement(atom, "ChapterUID").text = str(chapter.uid)
        ET.SubElement(atom, "ChapterTimeStart").text = format_timestamp(chapter.start_ns)
        if chapter.end_ns is not None:
            ET.SubElement(atom, "ChapterTimeEnd").text = format_timestamp(chapter.end_ns)
        ET.SubElement(atom, "ChapterFlagHidden").text = "1" if chapter.hidden else "0"
        ET.SubElement(atom, "ChapterFlagEnabled").text = "0" if not chapter.enabled else "1"
        if chapter.name:
            display = ET.SubElement(atom, "ChapterDisplay")
            ET.SubElement(display, "ChapterString").text = chapter.name
            ET.SubElement(display, "ChapterLanguage").text = bibliographic(
                chapter.language or language
            )
    ET.indent(root, space="  ")
    return _HEADER + ET.tostring(root, encoding="unicode") + "\n"


def rollback(chapters: ChapterSet | Iterable[Chapter]) -> str:
    """The document that undoes a naming pass: the same marks, no names."""
    chapter_set = (
        chapters if isinstance(chapters, ChapterSet) else ChapterSet(tuple(chapters))
    )
    return build(chapter_set.stripped())


# ------------------------------------------------------------------- the checks
def is_generic_name(name: str | None) -> bool:
    """True for a "name" that is only a label: a number, or a timecode."""
    if name is None:
        return True
    text = name.strip()
    if not text:
        return True
    return bool(
        _GENERIC_LABELLED.match(text)
        or _GENERIC_BARE.match(text)
        or _TIMECODE.match(text)
    )


def downstream_label(index: int) -> str:
    """What a consumer displays for a mark with no name, counting from one.

    A comparison between a document and what a consumer shows has to treat
    this as a match for an unnamed mark, or every correctly unnamed chapter
    reads as a mismatch.
    """
    return f"Chapter {index}"


def trim_trailing(
    chapters: ChapterSet, runtime_s: float
) -> tuple[ChapterSet, tuple[Chapter, ...]]:
    """Drop marks at or past the end of the feature; return them too.

    Exported chapter lists routinely carry one, and it is not an error in the
    list -- it is the end of the disc. It is an error in a file.
    """
    limit = int(runtime_s * 1_000_000_000)
    kept = tuple(c for c in chapters if c.start_ns < limit)
    dropped = tuple(c for c in chapters if c.start_ns >= limit)
    return replace(chapters, chapters=kept), dropped


def selfcheck(
    chapters: ChapterSet,
    *,
    runtime_s: float | None = None,
    expected_count: int | None = None,
    file_count: int | None = None,
) -> list[Problem]:
    """Every mechanical check, before anything is written.

    Each of these was earned by a document that passed the obvious ones. A
    blocking problem means the document describes a different cut -- or
    describes nothing -- and applying it would lose the positions the file
    already has.
    """
    problems: list[Problem] = []

    def reject(rule: str, message: str) -> None:
        problems.append(Problem("reject", rule, message))

    def note(rule: str, message: str) -> None:
        problems.append(Problem("note", rule, message))

    if not chapters.chapters:
        reject("empty", "the document has no marks")
        return problems
    if expected_count is not None and len(chapters) != expected_count:
        reject(
            "count",
            f"{len(chapters)} marks, {expected_count} expected from the source",
        )
    if file_count is not None and len(chapters) != file_count:
        reject(
            "file-count",
            f"{len(chapters)} marks against {file_count} in the file; applying this "
            f"would replace the element and lose {abs(file_count - len(chapters))} of them",
        )

    starts = chapters.starts_ns
    for position, (before, after) in enumerate(pairwise(starts), 1):
        if after < before:
            reject("order", f"mark {position + 1} is before mark {position}")
        elif after - before < SAME_MARK_NS:
            reject(
                "duplicate",
                f"marks {position} and {position + 1} are at the same timestamp",
            )

    hidden = [i for i, c in enumerate(chapters, 1) if c.hidden]
    if hidden:
        reject("hidden", f"mark(s) {hidden} are hidden and would not be shown")

    if starts[0] > FIRST_MARK_S * 1_000_000_000:
        note("first-mark", f"the first mark is at {chapters[0].start_s:.3f} s")

    if runtime_s is not None and runtime_s > 0:
        last = chapters[-1].start_s
        if last >= runtime_s:
            reject(
                "past-the-end",
                f"the last mark is at {last:.3f} s and the runtime is {runtime_s:.3f} s",
            )
        elif runtime_s - last < LAST_MARK_MARGIN_S:
            note(
                "near-the-end",
                f"the last mark is {runtime_s - last:.3f} s before the end",
            )

    for position, chapter in enumerate(chapters, 1):
        if chapter.name and REPLACEMENT_CHARACTER in chapter.name:
            reject(
                "encoding",
                f"the name of mark {position} contains a replacement character: "
                "the original byte is unrecoverable, so the name is not usable",
            )

    named = [c for c in chapters if c.name]
    if named and all(is_generic_name(c.name) for c in named):
        reject(
            "not-names",
            "every name is a label or a timecode; writing these is worse than "
            "leaving the marks unnamed, because nothing downstream can tell them "
            "from a label it made up itself",
        )
    return problems
