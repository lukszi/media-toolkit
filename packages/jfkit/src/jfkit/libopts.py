"""jfkit.libopts -- library options, including the ones the document leaves out.

A library's options are stored as a document beside the library, and the
document only contains the fields that differ from the type's constructor
defaults. Read it, change one field, send the whole object back, and every
field the document omitted goes back as *nothing* rather than as its default --
which is a different library from the one you started with, and it takes a
scan to notice.

So the defaults table is data here, with a test, and reading is
``defaults | document`` in that order. Everything else in this module follows
from that one rule.

**The identifier, and where it comes from.** The list-of-libraries route
matches each library to its stored record by comparing paths exactly. After
the server's data directory moves, those paths are stale, nothing matches,
and the route answers with an identifier of ``null`` and options of ``null``
for every library -- not an error, just an empty answer that reads as "this
library has no options". :func:`root_path_problems` is the two-line check
that says so, and it is the first thing to run when a plugin starts reporting
that a library has no valid identifier.

**One field at a time, and prove it.** :func:`patch` returns the new object
and the difference; :func:`write_options` sends it, re-reads the document from
disk and asserts that exactly the intended fields moved. Anything else is a
field the defaults table got wrong, and finding that out now is much cheaper
than finding it out from the next scan.

Two options are worth naming, because both change behaviour wholesale rather
than locally.

``EnableEmbeddedTitles`` makes every refresh overwrite an item's name with the
title stored inside the container. On a library assembled from released files
that title is the release name, so the effect is a library that re-derives its
own names from filenames every time anything is refreshed.

``ExtractChapterImagesDuringLibraryScan`` moves chapter-image extraction into
the scan, where it is a serial read over every file that changed. Turning it
on for a deliberate pass and off again afterwards is a pattern rather than a
setting, and :func:`toggled` is that pattern.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import json
import logging
import shutil
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any
from xml.etree import ElementTree

from .client import Client

__all__ = [
    "CTOR_DEFAULTS",
    "LibraryOptions",
    "OptionsWrite",
    "RootProblem",
    "backup_options",
    "libraries",
    "options_document",
    "parse_options",
    "patch",
    "read_options",
    "root_path_problems",
    "toggled",
    "write_options",
]

log = logging.getLogger(__name__)

#: Fields whose value is ``true``/``false`` in the document.
BOOLS: tuple[str, ...] = (
    "Enabled", "EnablePhotos", "EnableRealtimeMonitor", "EnableLUFSScan",
    "EnableChapterImageExtraction", "ExtractChapterImagesDuringLibraryScan",
    "EnableTrickplayImageExtraction", "ExtractTrickplayImagesDuringLibraryScan",
    "SaveLocalMetadata", "EnableInternetProviders", "EnableAutomaticSeriesGrouping",
    "EnableEmbeddedTitles", "EnableEmbeddedExtrasTitles", "EnableEmbeddedEpisodeInfos",
    "SkipSubtitlesIfEmbeddedSubtitlesPresent", "SkipSubtitlesIfAudioTrackMatches",
    "RequirePerfectSubtitleMatch", "SaveSubtitlesWithMedia", "SaveLyricsWithMedia",
    "SaveTrickplayWithMedia", "PreferNonstandardArtistsTag", "UseCustomTagDelimiters",
    "AutomaticallyAddToCollection",
)
INTS: tuple[str, ...] = ("AutomaticRefreshIntervalDays",)
STRINGS: tuple[str, ...] = (
    "PreferredMetadataLanguage", "MetadataCountryCode", "SeasonZeroDisplayName",
    "AllowEmbeddedSubtitles",
)
STRING_LISTS: tuple[str, ...] = (
    "MetadataSavers", "DisabledLocalMetadataReaders", "LocalMetadataReaderOrder",
    "DisabledSubtitleFetchers", "SubtitleFetcherOrder", "DisabledMediaSegmentProviders",
    "MediaSegmentProviderOrder", "SubtitleDownloadLanguages", "DisabledLyricFetchers",
    "LyricFetcherOrder", "CustomTagDelimiters", "DelimiterWhitelist",
)

#: The two fields whose value is a list of records rather than a scalar, and
#: the shape of one record in each: the element each entry is written as, and
#: each field's kind -- ``str``, ``int``, ``list`` (of strings), or a nested
#: ``(entry element, shape)`` pair. Anything a document carries beyond this
#: shape is reported as unknown, and a write refuses while any is present,
#: because sending back a record with a part dropped is the silent change
#: this module exists to prevent.
Shape = Mapping[str, Any]
IMAGE_OPTION: Shape = {"Type": str, "Limit": int, "MinWidth": int}
TYPE_OPTION: Shape = {
    "Type": str,
    "MetadataFetchers": list,
    "MetadataFetcherOrder": list,
    "ImageFetchers": list,
    "ImageFetcherOrder": list,
    "ImageOptions": ("ImageOption", IMAGE_OPTION),
}
PATH_INFO: Shape = {"Path": str, "NetworkPath": str}
RECORD_LISTS: Mapping[str, tuple[str, Shape]] = {
    "PathInfos": ("MediaPathInfo", PATH_INFO),
    "TypeOptions": ("TypeOptions", TYPE_OPTION),
}

#: What each field is when the document does not mention it. These are the
#: type's own constructor defaults; a field omitted from the document has this
#: value at runtime, and sending it back as ``null`` instead is the silent
#: change this table exists to prevent.
CTOR_DEFAULTS: Mapping[str, Any] = {
    "Enabled": True,
    "EnablePhotos": True,
    "EnableRealtimeMonitor": False,
    "EnableLUFSScan": False,
    "EnableChapterImageExtraction": False,
    "ExtractChapterImagesDuringLibraryScan": False,
    "EnableTrickplayImageExtraction": False,
    "ExtractTrickplayImagesDuringLibraryScan": False,
    "SaveLocalMetadata": False,
    "EnableInternetProviders": False,
    "EnableAutomaticSeriesGrouping": True,
    "EnableEmbeddedTitles": False,
    "EnableEmbeddedExtrasTitles": False,
    "EnableEmbeddedEpisodeInfos": False,
    "AutomaticRefreshIntervalDays": 0,
    "PreferredMetadataLanguage": None,
    "MetadataCountryCode": None,
    "SeasonZeroDisplayName": "Specials",
    "MetadataSavers": None,
    "DisabledLocalMetadataReaders": [],
    "LocalMetadataReaderOrder": None,
    "DisabledSubtitleFetchers": [],
    "SubtitleFetcherOrder": [],
    "DisabledMediaSegmentProviders": [],
    "MediaSegmentProviderOrder": [],
    "SkipSubtitlesIfEmbeddedSubtitlesPresent": False,
    "SkipSubtitlesIfAudioTrackMatches": True,
    "SubtitleDownloadLanguages": None,
    "RequirePerfectSubtitleMatch": True,
    "SaveSubtitlesWithMedia": True,
    "SaveLyricsWithMedia": False,
    "SaveTrickplayWithMedia": False,
    "DisabledLyricFetchers": [],
    "LyricFetcherOrder": [],
    "PreferNonstandardArtistsTag": False,
    "UseCustomTagDelimiters": False,
    "CustomTagDelimiters": ["/", "|", ";", "\\"],
    "DelimiterWhitelist": [],
    "AutomaticallyAddToCollection": False,
    "AllowEmbeddedSubtitles": "AllowAll",
    "PathInfos": [],
    "TypeOptions": [],
}

#: The file the effective options live in, inside a library's own directory.
OPTIONS_FILE = "options.xml"


@dataclass(frozen=True)
class LibraryOptions:
    """One library's options, with the omitted fields filled in."""

    library_id: str | None
    name: str
    values: Mapping[str, Any]
    source: Path | None = None
    #: elements the document carried that this table does not know about. A
    #: round-trip would drop them, so a write refuses while any are present.
    unknown: tuple[str, ...] = ()
    #: fields the document did not mention, filled from the defaults table
    defaulted: tuple[str, ...] = ()

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __str__(self) -> str:
        return (
            f"{self.name} ({self.library_id or 'no identifier'}): "
            f"{len(self.values)} field(s), {len(self.defaulted)} from defaults"
            + (f", {len(self.unknown)} unknown" if self.unknown else "")
        )


@dataclass(frozen=True)
class OptionsWrite:
    """What a write intended, and what the document said afterwards."""

    library_id: str
    intended: tuple[str, ...]
    applied: bool
    moved: tuple[str, ...] = ()
    backup: Path | None = None
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        head = "applied" if self.applied else "not applied (dry run)"
        lines = [f"{self.library_id}: {head}; intended {', '.join(self.intended)}"]
        if self.moved:
            lines.append(f"  on disk afterwards, changed: {', '.join(self.moved)}")
        if self.backup is not None:
            lines.append(f"  the document as it was is at {self.backup}")
        lines += [f"  problem: {p}" for p in self.problems]
        return "\n".join(lines)


@dataclass(frozen=True)
class RootProblem:
    """One library whose stored path no longer matches where the server looks."""

    name: str
    stored: str | None
    expected_under: str

    def __str__(self) -> str:
        return (
            f"{self.name}: recorded at {self.stored!r}, which is not under "
            f"{self.expected_under}"
        )


# ------------------------------------------------------------------ reading
def _strings(node: ElementTree.Element) -> list[str]:
    return [(child.text or "") for child in node.findall("string")]


def _records(
    node: ElementTree.Element, entry_tag: str, shape: Shape, where: str,
    unknown: list[str],
) -> list[dict[str, Any]]:
    """A list of records, parsed field by field against a known shape.

    Recursive, so a fetcher list stays a list and an image option stays a
    record with its numbers as numbers -- rather than being flattened to the
    whitespace between the elements, which is what reading ``.text`` does.
    Anything outside the shape is added to ``unknown`` under its full path.
    """
    out: list[dict[str, Any]] = []
    for entry in node:
        if entry.tag != entry_tag:
            unknown.append(f"{where}/{entry.tag}")
            continue
        record: dict[str, Any] = {}
        for sub in entry:
            kind = shape.get(sub.tag)
            here = f"{where}/{entry_tag}/{sub.tag}"
            if kind is None or sub.tag in record:
                unknown.append(here)
            elif kind is str:
                record[sub.tag] = sub.text
            elif kind is int:
                try:
                    record[sub.tag] = int((sub.text or "").strip())
                except ValueError:
                    unknown.append(here)
            elif kind is list:
                if any(child.tag != "string" for child in sub):
                    unknown.append(here)
                record[sub.tag] = _strings(sub)
            else:
                inner_tag, inner_shape = kind
                record[sub.tag] = _records(sub, inner_tag, inner_shape, here, unknown)
        out.append(record)
    return out


def _render_records(
    tag: str, records: Sequence[Mapping[str, Any]], entry_tag: str, shape: Shape
) -> ElementTree.Element:
    """The inverse of :func:`_records`, used only to prove the round-trip."""
    node = ElementTree.Element(tag)
    for record in records:
        entry = ElementTree.SubElement(node, entry_tag)
        for key, value in record.items():
            kind = shape[key]
            if isinstance(kind, tuple):
                entry.append(_render_records(key, value, *kind))
                continue
            sub = ElementTree.SubElement(entry, key)
            if kind is list:
                for item in value:
                    ElementTree.SubElement(sub, "string").text = item
            elif value is not None:
                sub.text = str(value)
    return node


def _canonical(node: ElementTree.Element) -> tuple[Any, ...]:
    """An element as a comparable value: tag, attributes, text, children."""
    return (
        node.tag,
        tuple(sorted(node.attrib.items())),
        (node.text or "").strip(),
        tuple(_canonical(child) for child in node),
    )


def parse_options(text: str) -> tuple[dict[str, Any], list[str], list[str]]:
    """Parse an options document into values, the fields it named, and the rest.

    The three return values are the whole point: what the options are, which
    of them the document actually carried, and which elements this table does
    not understand. A round-trip would drop the third group, so a caller that
    finds any refuses to write rather than silently discarding them.

    The two record lists -- paths and per-type options -- are parsed against
    their known shapes (:data:`RECORD_LISTS`) and then rendered back and
    compared with the document. Anything the shape does not cover, and any
    list that does not come back exactly as it went in, is reported as
    unknown, so a write refuses rather than send back a flattened copy.
    """
    root = ElementTree.fromstring(text)
    values: dict[str, Any] = copy.deepcopy(dict(CTOR_DEFAULTS))
    named: list[str] = []
    unknown: list[str] = []
    for child in root:
        tag = child.tag
        named.append(tag)
        if tag in BOOLS:
            values[tag] = (child.text or "").strip().lower() == "true"
        elif tag in INTS:
            values[tag] = int((child.text or "0").strip())
        elif tag in STRINGS:
            values[tag] = child.text
        elif tag in STRING_LISTS:
            values[tag] = _strings(child)
        elif tag in RECORD_LISTS:
            entry_tag, shape = RECORD_LISTS[tag]
            found: list[str] = []
            values[tag] = _records(child, entry_tag, shape, tag, found)
            unknown += found
            if not found and _canonical(
                _render_records(tag, values[tag], entry_tag, shape)
            ) != _canonical(child):
                # parsed without complaint and still does not come back as
                # it went in: an attribute, stray text -- refuse either way
                unknown.append(f"{tag} (does not round-trip)")
        else:
            unknown.append(tag)
    return values, named, unknown


def options_document(library_path: Path | str) -> Path:
    """Where a library's effective options live."""
    return Path(library_path) / OPTIONS_FILE


def read_options(
    path: Path | str, *, library_id: str | None = None, name: str = ""
) -> LibraryOptions:
    """Read the effective options, with the omitted fields filled in.

    The document is the source of truth rather than the list-of-libraries
    route, because that route answers with nothing at all when the stored
    paths are stale -- see :func:`root_path_problems`.
    """
    document = Path(path)
    values, named, unknown = parse_options(document.read_text(encoding="utf-8"))
    return LibraryOptions(
        library_id=library_id,
        name=name or document.parent.name,
        values=values,
        source=document,
        unknown=tuple(unknown),
        defaulted=tuple(sorted(set(CTOR_DEFAULTS) - set(named))),
    )


def libraries(client: Client) -> list[dict[str, Any]]:
    """Every library the server lists, as it lists them."""
    found = client.get("/Library/VirtualFolders")
    return list(found) if isinstance(found, list) else []


def root_path_problems(
    client: Client, *, data_dir: Path | str
) -> list[RootProblem]:
    """Libraries whose stored path is not under the directory the server uses.

    This is the check for a data directory that moved. The symptom nobody
    connects to the cause is a library listed with no identifier and no
    options -- because the list route matches by exact path, finds nothing,
    and returns the empty half of the record rather than an error. Plugins
    that need the identifier then report that the library is invalid, which
    sends everybody looking at the plugin.
    """
    root = PurePath(str(data_dir))
    problems: list[RootProblem] = []
    for library in libraries(client):
        stored = library.get("Path") or (library.get("Locations") or [None])[0]
        if library.get("ItemId") and library.get("LibraryOptions"):
            continue
        if stored is not None and PurePath(str(stored)).is_relative_to(root):
            continue
        problems.append(
            RootProblem(str(library.get("Name") or "?"), stored, str(root))
        )
    return problems


# ------------------------------------------------------------------ writing
def patch(
    options: LibraryOptions, changes: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The new object, and the fields that differ. No I/O."""
    unknown = sorted(set(changes) - set(options.values))
    if unknown:
        raise KeyError(f"not a library option: {', '.join(unknown)}")
    new = copy.deepcopy(dict(options.values))
    new.update(changes)
    moved = sorted(k for k in new if options.values.get(k) != new.get(k))
    return new, moved


def backup_options(options: LibraryOptions, directory: Path | str) -> Path | None:
    """Copy the document aside, stamped, before anything is sent."""
    if options.source is None:
        return None
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out / f"{options.name}.{stamp}.xml"
    shutil.copy2(options.source, target)
    (out / f"{options.name}.{stamp}.json").write_text(
        json.dumps(dict(options.values), ensure_ascii=False, indent=1, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def write_options(
    client: Client,
    options: LibraryOptions,
    changes: Mapping[str, Any],
    *,
    backup_dir: Path | str | None = None,
    expect: Sequence[str] | None = None,
) -> OptionsWrite:
    """Send the whole object back, then read the document and check what moved.

    The verification is not decoration. The failure this module exists to
    prevent is a field silently becoming something else because the defaults
    table was wrong about it, and the only way to know is to read the
    document afterwards and compare.
    """
    if options.library_id is None:
        raise ValueError(
            f"{options.name} has no identifier: the list route could not match it to "
            "its record, which usually means the stored paths are stale"
        )
    if options.unknown:
        raise ValueError(
            f"{options.name}: the document carries elements this table does not know "
            f"({', '.join(options.unknown)}); sending the object back would drop them"
        )
    new, moved = patch(options, changes)
    intended = tuple(expect if expect is not None else moved)
    backup = backup_options(options, backup_dir) if backup_dir is not None else None

    log.info("%s: would change %s", options.name, ", ".join(moved) or "nothing")
    client.post(
        "/Library/VirtualFolders/LibraryOptions",
        {"Id": options.library_id, "LibraryOptions": new},
    )
    if client.dry_run:
        return OptionsWrite(options.library_id, intended, False, tuple(moved), backup)

    problems: list[str] = []
    after_moved: tuple[str, ...] = ()
    if options.source is not None:
        after = read_options(
            options.source, library_id=options.library_id, name=options.name
        )
        after_moved = tuple(
            sorted(k for k in after.values if options.values.get(k) != after.get(k))
        )
        unexpected = sorted(set(after_moved) - set(intended))
        missing = sorted(set(intended) - set(after_moved))
        if unexpected:
            problems.append(f"fields moved that nobody asked about: {unexpected}")
        if missing:
            problems.append(f"fields that did not move: {missing}")
    return OptionsWrite(
        options.library_id, intended, True, after_moved, backup, tuple(problems)
    )


@contextmanager
def toggled(
    client: Client,
    options: LibraryOptions,
    field: str,
    value: Any,
    *,
    backup_dir: Path | str | None = None,
) -> Iterator[OptionsWrite]:
    """Set one option for the duration of a pass, and put it back afterwards.

    Some options are worth having on for one deliberate operation and wrong to
    leave on -- extraction during a scan above all, which turns every scan
    into a serial read of everything that changed. Doing that by hand means
    remembering to undo it, and a pass that fails halfway is a pass that does
    not.

    The restore runs even when the body raises, and it runs through the same
    verified write, so "it was put back" is a measurement too.
    """
    was = options.get(field)
    result = write_options(
        client, options, {field: value}, backup_dir=backup_dir, expect=[field]
    )
    try:
        yield result
    finally:
        current = (
            read_options(options.source, library_id=options.library_id,
                         name=options.name)
            if options.source is not None and not client.dry_run
            else options
        )
        write_options(client, current, {field: was}, expect=[field])
        log.info("%s: %s put back to %r", options.name, field, was)
