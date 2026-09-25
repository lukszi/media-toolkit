"""mkvkit.tags -- the tag element: what it can override, and how not to lose it.

A Matroska file can carry tags targeted at the whole file or at one track, and
a track-targeted language tag **wins over the track header** for the demuxer
most consumers are built on. That makes this element the difference between a
language edit that works and one that appears to work: setting the header of a
file that has such a tag changes nothing anybody sees, and the track table
will happily show you the new value while every player shows the old one.

Three rules follow, and all three are enforced here rather than remembered.

**A language change is two edits, or it is not a language change.** Set the
header, and rewrite any tag that contradicts it. :func:`set_track_language`
does the second half; :mod:`mkvkit.propedit` refuses the write that would do
only the first.

**Writing tags replaces all of them.** The editor takes one document and puts
it in place of the element. So the only safe way to add a tag is to read what
is there, merge into it, and write the whole thing back -- which is what
:func:`merge` is for. Never hand the editor a document you did not build from
the file's current one.

**Compare tags on the track identifier, not on the whole element.** The editor
re-emits the target block in its own normal form: it drops a default target
type value, it adds an explicit target type, and it writes a tag-language
element into every simple tag it touches. A comparison over the raw elements
therefore reports every tag as lost and re-added, every time, and a real loss
hides in that noise. :meth:`TagSet.triples` is the comparison that does not
lie: the track identifiers, the name and the value, sorted.

Provenance is the other reason this module exists. When a program writes
something into somebody's file -- names it did not invent, a language it
decided -- the file should say where that came from, in the file itself rather
than in a log somebody will lose. :func:`provenance` builds that tag, and
merging it twice leaves one.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .config import Config
from .langcodes import canonical
from .run import Runner, default_runner

__all__ = [
    "LANGUAGE_TAG",
    "PROVENANCE_SUFFIX",
    "SimpleTag",
    "Tag",
    "TagError",
    "TagSet",
    "Targets",
    "build",
    "merge",
    "parse",
    "provenance",
    "read_tags",
    "set_track_language",
]

#: The simple tag whose value overrules the track header.
LANGUAGE_TAG: Final = "LANGUAGE"
#: How a provenance tag is named: ``CHAPTER_NAMES_SOURCE`` and so on.
PROVENANCE_SUFFIX: Final = "_SOURCE"

_HEADER: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n'
)

#: Elements inside a target block, and the field each one fills.
_TARGET_FIELDS: Final[dict[str, str]] = {
    "TrackUID": "track_uids",
    "EditionUID": "edition_uids",
    "ChapterUID": "chapter_uids",
    "AttachmentUID": "attachment_uids",
}


class TagError(ValueError):
    """A tag document could not be read."""


@dataclass(frozen=True)
class SimpleTag:
    """One name and value, with everything else the element can carry.

    A simple tag can hold a binary value instead of a string, can say which
    language its value is in (two ways) and whether that is the default, and
    can hold simple tags of its own. None of that is ever written by this
    package, but all of it is read and written back: a tag document is
    replaced whole, so anything a read drops is deleted from the file by the
    next write.
    """

    name: str
    value: str = ""
    language: str | None = None
    language_ietf: str | None = None
    #: The element's "this is the default language" flag, as written ("0"/"1").
    default: str | None = None
    #: A binary value, exactly as the extractor printed it, and its format.
    binary: str | None = None
    binary_format: str | None = None
    children: tuple[SimpleTag, ...] = ()

    @property
    def is_language(self) -> bool:
        return self.name.upper() == LANGUAGE_TAG

    def flat(self, prefix: str = "") -> Iterable[tuple[str, str]]:
        """``(path, value)`` for this tag and every tag nested under it.

        A nested tag is named by its path (``ARTIST/SORT_WITH``), so a nested
        tag that came back flattened reads as a different tag, and a binary
        value is compared by its text, so one that came back empty is a loss.
        """
        path = f"{prefix}{self.name.upper()}"
        value = self.value if self.binary is None else f"binary:{self.binary}"
        yield path, value
        for child in self.children:
            yield from child.flat(f"{path}/")


@dataclass(frozen=True)
class Targets:
    """What a tag is about. Empty means the whole file."""

    track_uids: tuple[int, ...] = ()
    edition_uids: tuple[int, ...] = ()
    chapter_uids: tuple[int, ...] = ()
    attachment_uids: tuple[int, ...] = ()
    type_value: int | None = None
    type: str | None = None

    @property
    def key(self) -> str:
        """The identity a comparison may rely on: the track identifiers alone.

        Everything else in the block is rewritten by the editor in its own
        normal form, so including it would make every rewritten tag look like
        a different tag.
        """
        return "|".join(str(uid) for uid in sorted(self.track_uids))

    @property
    def is_global(self) -> bool:
        return not (
            self.track_uids
            or self.edition_uids
            or self.chapter_uids
            or self.attachment_uids
        )


@dataclass(frozen=True)
class Tag:
    """One target block and the simple tags under it."""

    targets: Targets = field(default_factory=Targets)
    simples: tuple[SimpleTag, ...] = ()

    def named(self, name: str) -> SimpleTag | None:
        for simple in self.simples:
            if simple.name.upper() == name.upper():
                return simple
        return None


@dataclass(frozen=True)
class TagSet:
    """Every tag in one file, in document order."""

    tags: tuple[Tag, ...] = ()
    #: Elements the reader met and this model does not carry. :func:`build`
    #: refuses such a set, because writing it back would delete them.
    unkept: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.tags)

    def __bool__(self) -> bool:
        return bool(self.tags)

    @property
    def triples(self) -> tuple[tuple[str, str, str], ...]:
        """``(track identifiers, name, value)`` for every simple tag, sorted.

        This is what a before-and-after comparison uses. Anything finer
        reports the editor's own normalisation as a change.
        """
        out = [
            (tag.targets.key, name, value)
            for tag in self.tags
            for simple in tag.simples
            for name, value in simple.flat()
        ]
        return tuple(sorted(out))

    def for_track(self, uid: int) -> tuple[Tag, ...]:
        return tuple(tag for tag in self.tags if uid in tag.targets.track_uids)

    def language_of(self, uid: int) -> str | None:
        """The language a tag claims for a track, canonicalised, or ``None``."""
        for tag in self.for_track(uid):
            simple = tag.named(LANGUAGE_TAG)
            if simple is not None:
                return canonical(simple.value)
        return None

    def tracks_with_language(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for tag in self.tags:
            simple = tag.named(LANGUAGE_TAG)
            if simple is None:
                continue
            for uid in tag.targets.track_uids:
                value = canonical(simple.value)
                if value is not None:
                    out[uid] = value
        return out


# ---------------------------------------------------------------------- reading
def parse(text: str | None) -> TagSet:
    """Read a tag document. Nothing at all is an empty set, not an error."""
    if not (text or "").strip():
        return TagSet()
    try:
        root = ET.fromstring((text or "").lstrip("﻿"))
    except ET.ParseError as exc:
        raise TagError(f"the tag document does not parse: {exc}") from exc
    return TagSet(
        tuple(_tag(element) for element in root.findall("Tag")),
        unkept=tuple(sorted(set(_unknown_elements(root)))),
    )


#: Every element this module reads, by the element it may appear in.
_KNOWN_CHILDREN: Final[dict[str, frozenset[str]]] = {
    "Tags": frozenset({"Tag"}),
    "Tag": frozenset({"Targets", "Simple"}),
    "Targets": frozenset({"TargetTypeValue", "TargetType", *_TARGET_FIELDS}),
    "Simple": frozenset(
        {"Name", "String", "Binary", "TagLanguage", "TagLanguageIETF",
         "DefaultLanguage", "Simple"}
    ),
}


def _unknown_elements(element: ET.Element) -> Iterable[str]:
    known = _KNOWN_CHILDREN.get(element.tag, frozenset())
    for child in element:
        if child.tag not in known:
            yield f"{element.tag}/{child.tag}"
        else:
            yield from _unknown_elements(child)


def _tag(element: ET.Element) -> Tag:
    block = element.find("Targets")
    values: dict[str, tuple[int, ...]] = dict.fromkeys(_TARGET_FIELDS.values(), ())
    type_value: int | None = None
    target_type: str | None = None
    if block is not None:
        for child in block:
            field_name = _TARGET_FIELDS.get(child.tag)
            if field_name is not None:
                number = _int(child.text)
                if number is not None:
                    values[field_name] = (*values[field_name], number)
            elif child.tag == "TargetTypeValue":
                type_value = _int(child.text)
            elif child.tag == "TargetType":
                target_type = (child.text or "").strip() or None
    targets = Targets(
        track_uids=values["track_uids"],
        edition_uids=values["edition_uids"],
        chapter_uids=values["chapter_uids"],
        attachment_uids=values["attachment_uids"],
        type_value=type_value,
        type=target_type,
    )
    # Simple elements nest, and the nesting is kept: the document is written
    # back whole, so a flattened read would flatten the file.
    simples = tuple(_simple(simple) for simple in element.findall("Simple"))
    return Tag(targets=targets, simples=simples)


def _simple(element: ET.Element) -> SimpleTag:
    binary = element.find("Binary")
    return SimpleTag(
        name=(element.findtext("Name") or "").strip(),
        value=(element.findtext("String") or "").strip(),
        language=_text(element, "TagLanguage"),
        language_ietf=_text(element, "TagLanguageIETF"),
        default=_text(element, "DefaultLanguage"),
        binary=None if binary is None else (binary.text or "").strip(),
        binary_format=None if binary is None else binary.get("format"),
        children=tuple(_simple(child) for child in element.findall("Simple")),
    )


def _text(element: ET.Element, name: str) -> str | None:
    return (element.findtext(name) or "").strip() or None


def _int(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def read_tags(
    path: Path | str, *, runner: Runner | None = None, config: Config | None = None
) -> TagSet:
    """The tags a file carries, as a set. An untagged file gives an empty one."""
    run = runner if runner is not None else default_runner(config)
    result = run("mkvextract", [str(path), "tags"], ok=(0, 1))
    return parse(result.stdout)


# ---------------------------------------------------------------------- writing
def build(tags: TagSet | Iterable[Tag]) -> str:
    """The document to hand the editor. Always built from everything, never a part."""
    tag_set = tags if isinstance(tags, TagSet) else TagSet(tuple(tags))
    if tag_set.unkept:
        raise TagError(
            "the tag document holds element(s) this module does not carry ("
            + ", ".join(tag_set.unkept)
            + "); writing it back would delete them, so it is not written"
        )
    root = ET.Element("Tags")
    for tag in tag_set.tags:
        element = ET.SubElement(root, "Tag")
        block = ET.SubElement(element, "Targets")
        if tag.targets.type_value is not None:
            ET.SubElement(block, "TargetTypeValue").text = str(tag.targets.type_value)
        if tag.targets.type:
            ET.SubElement(block, "TargetType").text = tag.targets.type
        for element_name, field_name in _TARGET_FIELDS.items():
            for uid in getattr(tag.targets, field_name):
                ET.SubElement(block, element_name).text = str(uid)
        for simple in tag.simples:
            _build_simple(element, simple)
    ET.indent(root, space="  ")
    return _HEADER + ET.tostring(root, encoding="unicode") + "\n"


def _build_simple(parent: ET.Element, simple: SimpleTag) -> None:
    node = ET.SubElement(parent, "Simple")
    ET.SubElement(node, "Name").text = simple.name
    if simple.binary is not None:
        binary = ET.SubElement(node, "Binary")
        binary.text = simple.binary
        if simple.binary_format:
            binary.set("format", simple.binary_format)
    else:
        ET.SubElement(node, "String").text = simple.value
    if simple.language:
        ET.SubElement(node, "TagLanguage").text = simple.language
    if simple.language_ietf:
        ET.SubElement(node, "TagLanguageIETF").text = simple.language_ietf
    if simple.default is not None:
        ET.SubElement(node, "DefaultLanguage").text = simple.default
    for child in simple.children:
        _build_simple(node, child)


def set_track_language(tags: TagSet, uid: int, language: str) -> TagSet:
    """Align any language tag on this track with the value being written.

    Only existing tags are rewritten. A track with no language tag needs none
    -- the header is what a consumer reads when there is nothing to override
    it -- and inventing one would add an element to somebody's file for no
    reason, which is its own kind of surprise.
    """
    changed: list[Tag] = []
    for tag in tags.tags:
        if uid not in tag.targets.track_uids or tag.named(LANGUAGE_TAG) is None:
            changed.append(tag)
            continue
        simples = tuple(
            replace(simple, value=language) if simple.is_language else simple
            for simple in tag.simples
        )
        changed.append(replace(tag, simples=simples))
    return TagSet(tuple(changed), unkept=tags.unkept)


def provenance(
    kind: str,
    source: str,
    *,
    when: dt.date | None = None,
    tool: str | None = None,
    targets: Targets | None = None,
) -> Tag:
    """A tag saying where something a program wrote into the file came from.

    ``kind`` names what was written (``chapter_names``, ``language``); the
    simple tags are ``<KIND>_SOURCE`` and, where given, a date and the tool.
    A file that has been edited should be able to say so without reference to
    a log file somebody will delete.
    """
    prefix = kind.strip().upper().replace(" ", "_").replace("-", "_")
    simples = [SimpleTag(f"{prefix}{PROVENANCE_SUFFIX}", source)]
    if when is not None:
        simples.append(SimpleTag(f"{prefix}{PROVENANCE_SUFFIX}_DATE", when.isoformat()))
    if tool:
        simples.append(SimpleTag(f"{prefix}{PROVENANCE_SUFFIX}_TOOL", tool))
    return Tag(targets=targets or Targets(), simples=tuple(simples))


def merge(existing: TagSet, additions: Sequence[Tag]) -> TagSet:
    """Everything already there, plus these, with any earlier copy replaced.

    Running a pass twice must leave the file as it was after the first run, so
    a simple tag with a name one of ``additions`` uses, on the same target, is
    dropped before the new one goes in. Nothing else is touched -- least of
    all a language tag, which may be the only thing making the file read
    correctly.
    """
    replaced_names = {
        (tag.targets.key, simple.name.upper())
        for tag in additions
        for simple in tag.simples
    }
    kept: list[Tag] = []
    for tag in existing.tags:
        simples = tuple(
            simple
            for simple in tag.simples
            if (tag.targets.key, simple.name.upper()) not in replaced_names
        )
        if simples:
            kept.append(replace(tag, simples=simples))
    return TagSet((*kept, *additions), unkept=existing.unkept)
