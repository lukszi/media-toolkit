"""jfkit.safedelete.junk -- what a file left in a media tree is, by rules a person can read.

A release arrives with more than its video: a tracker's note, a shortcut to
the tracker's site, the padding a client wrote to align pieces, a folder of
screenshots that proved the encode. None of it is read by the server and
none of it is anybody's content. Beside it, in the same folders, sit things
that look just as unimportant and are not: the description file the server
reads, the artwork it shows, the preview tiles it built, a subtitle that is
the only copy of a translation, a soundtrack somebody kept on purpose.

So every file is put in exactly one class, and the classes are asymmetric on
purpose:

* **JUNK** only where a rule names the file positively -- a tracker note by
  its name, a shortcut or program by its kind, padding by the client's own
  naming, a screenshot by its folder or its name. The rules are data
  (:class:`Rules`) with a conservative default and can be widened from a file.
* **PROTECTED** for every kind the server or the owner uses: description
  files, the artwork names the server reads, preview tiles, subtitles, audio,
  theme music, chapter documents, documents and archives, anything inside a
  disc structure, and the server's own ``.ignore`` marker. No rule can make a
  protected file junk: protection is checked first.
* **VIDEO** and **SAMPLE** for what the server would treat as an item, a
  release sample being a video named or filed as one. A sample is never
  junk; it is listed on its own and moves only when somebody released it.
* **UNSURE** for everything else, with the reason. A text file no tracker
  pattern names is UNSURE, not junk: it may be somebody's notes.
* **IGNORED** for the operating system's own folder files, which are neither
  content nor worth a person's attention.

Nothing here reads a file's contents or touches the disk beyond the name; the
caller passes the path relative to the library folder so the rules can see
the folders a file sits in.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import fnmatch
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from mkvkit.sidecars import (
    AUDIO_SUFFIXES,
    IMAGE_SUFFIXES,
    SUBTITLE_SUFFIXES,
    TRICKPLAY_SUFFIX,
    VIDEO_SUFFIXES,
)

__all__ = [
    "ARTWORK_FOLDERS",
    "DEFAULT_RULES",
    "DISC_FOLDERS",
    "Kind",
    "Rules",
    "Verdict",
    "classify",
    "is_protected_folder",
    "load_rules",
    "summarise",
]


class Kind(Enum):
    VIDEO = "video"
    SAMPLE = "sample"
    JUNK = "junk"
    PROTECTED = "protected"
    UNSURE = "unsure"
    IGNORED = "ignored"


@dataclass(frozen=True)
class Verdict:
    """The class of one file, and the rule that put it there."""

    kind: Kind
    reason: str

    @property
    def is_video(self) -> bool:
        return self.kind in (Kind.VIDEO, Kind.SAMPLE)

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.reason}"


#: Folders whose contents belong to a disc image laid out as files. Every file
#: in one is part of the disc, whatever its own name says.
DISC_FOLDERS = frozenset({"bdmv", "video_ts", "audio_ts", "hvdvd_ts", "certificate"})

#: Folders the server reads artwork, extra pictures or theme media from. Their
#: contents belong to the item beside them even though nothing in them is a
#: video.
ARTWORK_FOLDERS = frozenset({
    "metadata", "extrafanart", "extrathumbs", ".actors", "theme-music", "backdrops",
})

#: The picture names the server reads for a folder's item, optionally numbered
#: (``backdrop2``, ``fanart-1``). ``season01-poster`` and friends are below.
_ARTWORK_STEMS = (
    "folder", "poster", "cover", "default", "backdrop", "background", "fanart",
    "art", "banner", "logo", "clearlogo", "clearart", "landscape", "thumb",
    "disc", "discart", "cdart", "movie", "show", "characterart", "keyart",
)
_ARTWORK = re.compile(
    r"(?:" + "|".join(_ARTWORK_STEMS) + r")(?:-?\d+)?"
    r"|season(?:\d+|-all|-specials)?(?:-(?:poster|banner|landscape|fanart|thumb))?"
    r"|.+-(?:poster|folder|cover|default|logo|clearlogo|clearart|cdart|disc|discart"
    r"|banner|landscape|thumb|fanart|background|art|backdrop)(?:-?\d+)?",
    re.IGNORECASE,
)

_DOCUMENTS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".odp", ".rtf",
    ".md", ".htm", ".html", ".epub", ".csv", ".tsv", ".ppt", ".pptx",
})
_ARCHIVES = frozenset({
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".cbz", ".cbr", ".cb7",
})
#: Documents the toolkit or the server write beside a video.
_SIDECAR_DATA = frozenset({".xml", ".edl", ".bif", ".chapters"})
_UNFINISHED = frozenset({".part", ".parts", ".partial", ".crdownload", ".!qb", ".!ut"})
_OS_FILES = frozenset({"desktop.ini", "thumbs.db", ".ds_store", "ehthumbs.db"})


@dataclass(frozen=True)
class Rules:
    """What counts as release junk. Every pattern is a case-insensitive glob.

    The default is deliberately narrow. A person who knows their collection
    widens it from a file (:func:`load_rules`); nothing widens it silently.
    """

    #: text files that are tracker notes, by name
    tracker_notes: tuple[str, ...] = (
        "*torrent downloaded from*.txt", "*downloaded from*.txt",
        "*torrentgalaxy*.txt", "*upcoming releases*.txt", "*yts*.txt",
        "*yify*.txt", "*1337x*.txt", "*eztv*.txt", "*ettv*.txt", "www.*.txt",
        "bilgi*.txt",
    )
    #: shortcuts to a web site or a program
    shortcut_suffixes: tuple[str, ...] = (".url", ".website", ".lnk", ".webloc", ".desktop")
    #: programs, which have no business in a media folder
    program_suffixes: tuple[str, ...] = (".exe", ".bat", ".cmd", ".scr", ".pif", ".vbs", ".msi")
    #: release checksum lists
    checksum_suffixes: tuple[str, ...] = (".sfv", ".md5", ".sha1", ".sha256", ".par2")
    #: the files a torrent client writes to align pieces, by name
    padding_files: tuple[str, ...] = ("_____padding_file_*",)
    #: the folders a torrent client writes padding into
    padding_folders: tuple[str, ...] = (".pad",)
    #: folders holding a release's screenshots
    screenshot_folders: tuple[str, ...] = (
        "screens", "screenshots", "screenshot", "proof", "proofs", "snapshots",
    )
    #: pictures that are screenshots by their own name
    screenshot_files: tuple[str, ...] = ("*screenshot*", "proof*", "*-proof.*", "*.proof.*")
    #: video names that make a video a release sample
    sample_files: tuple[str, ...] = (
        "sample.*", "sample-*", "sample_*", "*-sample.*", "*.sample.*", "*_sample.*",
        "*- sample.*", "*.sample-*",
    )
    #: folders whose videos are release samples
    sample_folders: tuple[str, ...] = ("sample", "samples")

    def merged(self, raw: Mapping[str, Any]) -> Rules:
        """These rules with a file's section applied: ``key`` replaces, ``extra_key`` adds."""
        known = {f.name for f in fields(self)}
        problems: list[str] = []
        changes: dict[str, tuple[str, ...]] = {}
        for key, value in raw.items():
            name = key[len("extra_"):] if key.startswith("extra_") else key
            if name not in known:
                problems.append(f"unknown key {key!r}")
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                problems.append(f"{key} must be a list of strings")
                continue
            base = changes.get(name, getattr(self, name))
            changes[name] = (*base, *value) if key.startswith("extra_") else tuple(value)
        if problems:
            raise ValueError("; ".join(problems))
        return replace(self, **changes)

    def as_dict(self) -> dict[str, list[str]]:
        return {f.name: list(getattr(self, f.name)) for f in fields(self)}


DEFAULT_RULES = Rules()


def load_rules(path: Path | str | None) -> Rules:
    """The default rules, or the default with a file's ``[leftovers]`` table applied."""
    if path is None:
        return DEFAULT_RULES
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    section = raw.get("leftovers", raw)
    if not isinstance(section, dict):
        raise ValueError(f"{path}: [leftovers] must be a table")
    return DEFAULT_RULES.merged(section)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, p.casefold()) for p in patterns)


def _suffix(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def is_protected_folder(name: str) -> bool:
    """A folder the server or a disc owns, which is never a leftover itself."""
    folded = name.casefold()
    return (
        folded in DISC_FOLDERS or folded in ARTWORK_FOLDERS
        or folded.endswith(TRICKPLAY_SUFFIX)
    )


def classify(
    relative: PurePath | str,
    *,
    rules: Rules = DEFAULT_RULES,
    size: int | None = None,
) -> Verdict:
    """The class of one file, from its path below the library folder.

    ``relative`` must include the folders the file sits in: a picture is a
    screenshot because of its folder, and a file inside a disc structure or a
    preview-tile folder is protected because of where it is. ``size`` lets an
    empty file be told apart; it is never needed for protection.
    """
    path = PurePath(relative)
    name = path.name
    folded = name.casefold()
    suffix = _suffix(name)
    stem = name[: len(name) - len(suffix)] if suffix else name
    parents = [part.casefold() for part in path.parts[:-1]]
    parent = parents[-1] if parents else ""

    # -- protection first: nothing below can turn a protected file into junk
    if folded in _OS_FILES:
        return Verdict(Kind.IGNORED, "the operating system's own folder file")
    if folded == ".ignore":
        return Verdict(Kind.PROTECTED, "the server's marker to skip this folder")
    if any(part in DISC_FOLDERS for part in parents):
        return Verdict(Kind.PROTECTED, "part of a disc structure")
    if any(part.endswith(TRICKPLAY_SUFFIX) for part in parents):
        return Verdict(Kind.PROTECTED, "preview tiles the server built")
    if suffix in VIDEO_SUFFIXES:
        if _matches(name, rules.sample_files) or any(
            p in {s.casefold() for s in rules.sample_folders} for p in parents
        ):
            return Verdict(Kind.SAMPLE, "a release sample")
        return Verdict(Kind.VIDEO, "a video the server reads as an item")
    if stem.casefold() == "theme" or any(p in {"theme-music", "backdrops"} for p in parents):
        return Verdict(Kind.PROTECTED, "theme media")
    if suffix == ".nfo":
        return Verdict(Kind.PROTECTED, "a description file")
    if suffix in SUBTITLE_SUFFIXES:
        return Verdict(Kind.PROTECTED, "a subtitle")
    if suffix in AUDIO_SUFFIXES:
        return Verdict(Kind.PROTECTED, "an audio track")
    if suffix in _SIDECAR_DATA:
        return Verdict(Kind.PROTECTED, "a sidecar document (chapters, edit list, tiles)")
    if suffix in IMAGE_SUFFIXES and (
        _ARTWORK.fullmatch(stem) is not None
        or any(p in ARTWORK_FOLDERS for p in parents)
    ):
        return Verdict(Kind.PROTECTED, "artwork the server reads")
    if suffix in _DOCUMENTS:
        return Verdict(Kind.PROTECTED, "a document")
    if suffix in _ARCHIVES:
        return Verdict(Kind.PROTECTED, "an archive")

    # -- junk only where a rule names it
    if _matches(name, rules.padding_files) or any(
        p in {s.casefold() for s in rules.padding_folders} for p in parents
    ):
        return Verdict(Kind.JUNK, "torrent padding")
    if suffix in {s.lower() for s in rules.shortcut_suffixes}:
        return Verdict(Kind.JUNK, "a shortcut")
    if suffix in {s.lower() for s in rules.program_suffixes}:
        return Verdict(Kind.JUNK, "a program")
    if suffix in {s.lower() for s in rules.checksum_suffixes}:
        return Verdict(Kind.JUNK, "a release checksum list")
    if suffix == ".txt":
        if _matches(name, rules.tracker_notes):
            return Verdict(Kind.JUNK, "a tracker note")
        return Verdict(Kind.UNSURE, "a text file no tracker pattern names")
    if suffix in IMAGE_SUFFIXES:
        if parent in {s.casefold() for s in rules.screenshot_folders} or _matches(
            name, rules.screenshot_files
        ):
            return Verdict(Kind.JUNK, "a release screenshot")
        return Verdict(Kind.UNSURE, "a picture that is neither artwork nor a screenshot")
    if suffix in _UNFINISHED:
        return Verdict(Kind.UNSURE, "an unfinished download")
    if size == 0:
        return Verdict(Kind.UNSURE, "an empty file")
    return Verdict(Kind.UNSURE, "no rule knows this kind of file")


def summarise(verdicts: Sequence[Verdict]) -> dict[str, int]:
    """How many files of each class, for a one-line report."""
    out: dict[str, int] = {}
    for verdict in verdicts:
        out[verdict.kind.value] = out.get(verdict.kind.value, 0) + 1
    return out

