"""mkvkit.sidecars -- every file that belongs to a video, so a move can carry them all.

A video in a media library is rarely one file. The server reads a metadata
document, pictures and subtitles from beside it, and writes preview tiles
next to it. Rename the video alone and all of that is left behind, attached to
nothing: the pictures and the metadata stop applying, a subtitle stops being
offered, and the preview tiles for the old name are regenerated from scratch
under the new one. Park a video alone and the leftovers are what the next
cleanup has to explain.

This module answers one question -- *which files belong to this video* -- from
one directory listing, so that a rename, a move or a parking step can take the
whole set.

**The rules.** A video's *stem* is its file name without the last extension.
A file in the same folder belongs to the video when its name starts with the
stem (compared without regard to case, as the server compares it) and the
next character is ``.`` or ``-``. What follows the stem is the file's *tail*;
a rename keeps the tail and replaces the stem. Of the files that qualify:

* ``<stem>.trickplay`` is the preview-tile **folder** the server writes beside
  the video when it is told to keep them with the media. It is the only
  folder that belongs to a video.
* ``<stem>.nfo`` is the metadata document (``nfo``).
* A picture (``.png``, ``.jpg``, ``.jpeg``, ``.webp``, ``.tbn``, ``.gif``,
  ``.svg``) is an ``image``. The server reads ``<stem>.<ext>`` and
  ``<stem>-thumb.<ext>`` as the primary picture, and ``<stem>-poster``,
  ``-logo``, ``-clearart``, ``-disc``, ``-banner``, ``-landscape``, ``-thumb``,
  ``-fanart``, ``-backdrop`` and their numbered forms as the other kinds
  (``LocalImageProvider`` and ``EpisodeLocalImageProvider`` in Jellyfin 12.1).
  An episode's picture may also sit in a ``metadata`` folder beside it, as
  ``metadata/<stem>.<ext>``; that one is claimed too.
* A subtitle (``.ass``, ``.mks``, ``.sami``, ``.smi``, ``.srt``, ``.ssa``,
  ``.sub``, ``.sup``, ``.vtt``, and the VobSub ``.idx``) is a ``subtitle``, and
  an audio file (``.mka``, ``.ac3``, ``.dts``, ``.flac`` and the rest of the
  server's audio list) is ``audio``. The server reads them only when the tail
  starts with ``.`` -- ``<stem>.<lang>[.forced][.default].srt`` -- because its
  only flag delimiter is the dot (``MediaInfoResolver`` in
  MediaBrowser.Providers, and ``NamingOptions.SubtitleFileExtensions``,
  ``AudioFileExtensions`` and ``MediaFlagDelimiters`` in Emby.Naming).
* ``<stem>...xml`` is a ``chapters`` document, which is what this package
  writes. The server does not read it.
* Anything else that qualifies -- a ``.txt``, a ``.ttml``, an ``.edl``, a
  ``.bif`` -- is ``other``. The server ignores it, but it was named after the
  video by somebody, and a rename that leaves it behind leaves it orphaned.

Each :class:`Sidecar` says whether the server reads it (``read_by_server``),
so a caller can tell "must move with the video" from "should".

**Another video is never a sidecar,** and a file belongs to exactly one video:
the one with the **longest** stem that claims it. In a folder holding
``Harbour.Lights.S01E02.mkv`` and ``Harbour.Lights.S01E02.German.DL.mkv``,
the file ``Harbour.Lights.S01E02.German.DL.srt`` belongs to the second video,
not the first, although both stems prefix it. The server makes no such
distinction and offers the subtitle on both; a rename cannot move one file to
two places, and the longer stem is the one it was named after.

**One listing per folder.** :func:`sidecars_of` reads the folder once (and
the ``metadata`` folder once, only if there is one). A caller handling many
videos in one folder reads a :class:`FolderListing` once and passes it in,
or calls :func:`sidecars_in_folder`, which also returns the files that belong
to no video at all.

    >>> from pathlib import Path
    >>> from mkvkit.sidecars import rename_target
    >>> rename_target(Path("/srv/media/series/Northwind.S01E03E04-thumb.jpg"),
    ...               old_stem="Northwind.S01E03E04",
    ...               new_stem="Northwind - S01E03 - The Quiet Harbour").name
    'Northwind - S01E03 - The Quiet Harbour-thumb.jpg'
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "AUDIO_SUFFIXES",
    "IMAGE_SUFFIXES",
    "SUBTITLE_SUFFIXES",
    "VIDEO_SUFFIXES",
    "FolderListing",
    "FolderSidecars",
    "Sidecar",
    "SidecarKind",
    "SidecarSet",
    "iter_sets",
    "planned_renames",
    "rename_target",
    "sidecars_in_folder",
    "sidecars_of",
]

#: ``NamingOptions.VideoFileExtensions``, Jellyfin 12.1 (Emby.Naming/Common/
#: NamingOptions.cs). A file with one of these is an item of its own.
VIDEO_SUFFIXES = frozenset({
    ".001", ".3g2", ".3gp", ".amv", ".asf", ".asx", ".avi", ".bin", ".bivx",
    ".divx", ".dv", ".dvr-ms", ".f4v", ".fli", ".flv", ".ifo", ".img", ".iso",
    ".m2t", ".m2ts", ".m2v", ".m4v", ".mkv", ".mk3d", ".mov", ".mp4", ".mpe",
    ".mpeg", ".mpg", ".mts", ".mxf", ".nrg", ".nsv", ".nuv", ".ogm", ".ogv",
    ".pva", ".qt", ".rec", ".rm", ".rmvb", ".strm", ".svq3", ".tp", ".ts",
    ".ty", ".viv", ".vob", ".vp3", ".webm", ".wmv", ".wtv", ".xvid",
})

#: ``NamingOptions.SubtitleFileExtensions`` plus the VobSub index, which the
#: external-file parser recognises separately (ExternalPathParser.cs).
SUBTITLE_SUFFIXES = frozenset({
    ".ass", ".mks", ".sami", ".smi", ".srt", ".ssa", ".sub", ".sup", ".vtt", ".idx",
})

#: ``NamingOptions.AudioFileExtensions``, Jellyfin 12.1.
AUDIO_SUFFIXES = frozenset({
    ".669", ".3gp", ".aa", ".aac", ".aax", ".ac3", ".act", ".adp", ".adplug",
    ".adx", ".afc", ".amf", ".aif", ".aifc", ".aiff", ".alac", ".amr", ".ape",
    ".ast", ".au", ".awb", ".cda", ".cue", ".dmf", ".dsf", ".dsm", ".dsp",
    ".dts", ".dvf", ".eac3", ".ec3", ".far", ".flac", ".gdm", ".gsm", ".gym",
    ".hps", ".imf", ".it", ".m15", ".m4a", ".m4b", ".mac", ".med", ".mka",
    ".mmf", ".mod", ".mogg", ".mp2", ".mp3", ".mpa", ".mpc", ".mpp", ".mp+",
    ".msv", ".nmf", ".nsf", ".nsv", ".oga", ".ogg", ".okt", ".opus", ".pls",
    ".ra", ".rf64", ".rm", ".s3m", ".sfx", ".shn", ".sid", ".stm", ".strm",
    ".ult", ".uni", ".vox", ".wav", ".wma", ".wv", ".xm", ".xsp", ".ymf",
}) - VIDEO_SUFFIXES

#: ``BaseItem.SupportedImageExtensions``.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tbn", ".gif", ".svg"})

#: The folder the server writes preview tiles into, beside the video.
TRICKPLAY_SUFFIX = ".trickplay"

#: The folder an episode's picture may be kept in, beside the video.
METADATA_FOLDER = "metadata"

#: The characters that may follow a stem in a file that belongs to it.
DELIMITERS = (".", "-")

#: The picture names the local image providers read, after ``<stem>``.
_READ_IMAGE = re.compile(
    r"(?:-(?:poster|folder|cover|default|logo|clearlogo|clearart|cdart|disc|"
    r"discart|banner|landscape|thumb|fanart|background|art|backdrop)(?:-?\d+)?)?",
    re.IGNORECASE,
)


class SidecarKind(Enum):
    NFO = "nfo"
    IMAGE = "image"
    TRICKPLAY = "trickplay"
    SUBTITLE = "subtitle"
    AUDIO = "audio"
    CHAPTERS = "chapters"
    OTHER = "other"


@dataclass(frozen=True)
class Sidecar:
    """One file (or the preview-tile folder) that belongs to a video.

    ``tail`` is everything after the stem, extension included -- for a
    picture in the ``metadata`` folder, after the stem of its own name --
    and ``in_metadata_folder`` says where it lives.
    """

    path: Path
    kind: SidecarKind
    tail: str
    read_by_server: bool
    is_dir: bool = False
    in_metadata_folder: bool = False

    def renamed(self, new_stem: str, new_folder: Path | None = None) -> Path:
        """Where this file goes when its video is renamed to ``new_stem``."""
        folder = new_folder if new_folder is not None else (
            self.path.parent.parent if self.in_metadata_folder else self.path.parent
        )
        if self.in_metadata_folder:
            folder = folder / self.path.parent.name
        return folder / f"{new_stem}{self.tail}"


@dataclass(frozen=True)
class SidecarSet:
    """A video and everything that belongs to it."""

    video: Path
    sidecars: tuple[Sidecar, ...] = ()

    @property
    def stem(self) -> str:
        return _stem(self.video.name)

    def __iter__(self) -> Iterator[Sidecar]:
        return iter(self.sidecars)

    def __len__(self) -> int:
        return len(self.sidecars)

    def of_kind(self, *kinds: SidecarKind) -> tuple[Sidecar, ...]:
        return tuple(s for s in self.sidecars if s.kind in kinds)

    @property
    def paths(self) -> tuple[Path, ...]:
        """The video first, then every sidecar, in name order."""
        return (self.video, *(s.path for s in self.sidecars))

    def renames(self, new_video: Path) -> list[tuple[Path, Path]]:
        """Every (from, to) pair a rename to ``new_video`` implies, video first.

        ``new_video`` may be in another folder: the set moves with it, and a
        picture in the ``metadata`` folder lands in the ``metadata`` folder
        beside the new name.
        """
        new_stem = _stem(new_video.name)
        pairs = [(self.video, new_video)]
        pairs += [(s.path, s.renamed(new_stem, new_video.parent)) for s in self.sidecars]
        return pairs

    def as_dict(self) -> dict[str, object]:
        return {
            "video": str(self.video),
            "sidecars": [
                {
                    "path": str(s.path), "kind": s.kind.value, "tail": s.tail,
                    "read_by_server": s.read_by_server, "is_dir": s.is_dir,
                }
                for s in self.sidecars
            ],
        }


@dataclass(frozen=True)
class FolderListing:
    """One folder's names, read once: ``(name, is_dir)`` pairs.

    ``metadata`` is the listing of a ``metadata`` folder beside the videos,
    when there is one, read at the same time.
    """

    folder: Path
    entries: tuple[tuple[str, bool], ...]
    metadata: tuple[str, ...] = ()

    @classmethod
    def read(cls, folder: str | os.PathLike[str]) -> FolderListing:
        here = Path(folder)
        entries = _scan(here)
        metadata: tuple[str, ...] = ()
        for name, is_dir in entries:
            if is_dir and name.casefold() == METADATA_FOLDER:
                metadata = tuple(n for n, d in _scan(here / name) if not d)
                break
        return cls(here, entries, metadata)

    def videos(self) -> list[str]:
        return [
            name for name, is_dir in self.entries
            if not is_dir and _suffix(name) in VIDEO_SUFFIXES
        ]


@dataclass(frozen=True)
class FolderSidecars:
    """Every video in a folder with its set, and the files nobody claims."""

    folder: Path
    sets: dict[Path, SidecarSet] = field(default_factory=dict)
    unclaimed: tuple[Path, ...] = ()


def _scan(folder: Path) -> tuple[tuple[str, bool], ...]:
    with os.scandir(folder) as listing:
        return tuple(sorted(
            (entry.name, entry.is_dir(follow_symlinks=False)) for entry in listing
        ))


def _suffix(name: str) -> str:
    # not os.path.splitext: a tail such as ".nfo" is all suffix, where
    # splitext would call it a hidden file with none
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _stem(name: str) -> str:
    return os.path.splitext(name)[0]


def _claims(stem: str, name: str) -> bool:
    """Whether a name starts with this stem followed by a delimiter."""
    folded, key = name.casefold(), stem.casefold()
    return (
        len(folded) > len(key)
        and folded.startswith(key)
        and folded[len(key)] in DELIMITERS
    )


def _classify(tail: str, is_dir: bool) -> tuple[SidecarKind, bool] | None:
    """The kind of a claimed entry, and whether the server reads it."""
    if is_dir:
        if tail.casefold() == TRICKPLAY_SUFFIX:
            return SidecarKind.TRICKPLAY, True
        return None
    suffix = _suffix(tail)
    before = tail[: len(tail) - len(suffix)] if suffix else tail
    if suffix == ".nfo":
        return SidecarKind.NFO, before == ""
    if suffix in IMAGE_SUFFIXES:
        return SidecarKind.IMAGE, _READ_IMAGE.fullmatch(before) is not None
    if suffix in VIDEO_SUFFIXES:
        return None
    dotted = tail.startswith(".")
    if suffix in SUBTITLE_SUFFIXES:
        return SidecarKind.SUBTITLE, dotted
    if suffix in AUDIO_SUFFIXES:
        return SidecarKind.AUDIO, dotted
    if suffix == ".xml":
        return SidecarKind.CHAPTERS, False
    return SidecarKind.OTHER, False


def _owner(name: str, stems: Sequence[str]) -> str | None:
    """The longest stem that claims a name, if any does."""
    best: str | None = None
    for stem in stems:
        if _claims(stem, name) and (best is None or len(stem) > len(best)):
            best = stem
    return best


def _build(listing: FolderListing) -> tuple[dict[str, list[Sidecar]], list[Path], list[str]]:
    videos = listing.videos()
    stems = [_stem(v) for v in videos]
    owned: dict[str, list[Sidecar]] = {stem: [] for stem in stems}
    unclaimed: list[Path] = []
    video_names = {v.casefold() for v in videos}

    for name, is_dir in listing.entries:
        if name.casefold() in video_names:
            continue
        if is_dir and name.casefold() == METADATA_FOLDER:
            continue
        owner = _owner(name, stems)
        found = None if owner is None else _classify(name[len(owner):], is_dir)
        if owner is None or found is None:
            if not is_dir and _suffix(name) not in VIDEO_SUFFIXES:
                unclaimed.append(listing.folder / name)
            continue
        kind, read = found
        owned[owner].append(
            Sidecar(listing.folder / name, kind, name[len(owner):], read, is_dir)
        )

    folded_stems = {stem.casefold(): stem for stem in stems}
    for name in listing.metadata:
        if _suffix(name) not in IMAGE_SUFFIXES:
            continue
        stem = folded_stems.get(_stem(name).casefold())
        if stem is None:
            continue
        owned[stem].append(Sidecar(
            listing.folder / METADATA_FOLDER / name, SidecarKind.IMAGE,
            name[len(stem):], True, False, True,
        ))
    return owned, unclaimed, videos


def sidecars_of(
    video: str | os.PathLike[str], *, listing: FolderListing | None = None,
) -> SidecarSet:
    """Every file that belongs to ``video``, by the rules in the module notes.

    The video does not have to exist -- a listing of its folder is enough --
    which is what lets a caller check a set again after a partial move.
    """
    path = Path(video)
    here = listing if listing is not None else FolderListing.read(path.parent)
    owned, _unclaimed, videos = _build(here)
    stem = _stem(path.name)
    if path.name.casefold() not in {v.casefold() for v in videos}:
        # a video not (or no longer) in the listing still has its stem
        extra = FolderListing(
            here.folder, (*here.entries, (path.name, False)), here.metadata
        )
        owned, _unclaimed, _videos = _build(extra)
    key = next((s for s in owned if s.casefold() == stem.casefold()), stem)
    return SidecarSet(path, tuple(sorted(owned.get(key, []), key=lambda s: str(s.path))))


def sidecars_in_folder(folder: str | os.PathLike[str]) -> FolderSidecars:
    """Every video in one folder with its set, from one listing.

    ``unclaimed`` holds the files (not folders) that belong to no video and
    are not videos themselves: the leftovers of something that was moved
    without them, or files that never belonged to anything.
    """
    listing = FolderListing.read(folder)
    owned, unclaimed, videos = _build(listing)
    sets = {
        listing.folder / name: SidecarSet(
            listing.folder / name,
            tuple(sorted(owned[_stem(name)], key=lambda s: str(s.path))),
        )
        for name in videos
    }
    return FolderSidecars(listing.folder, sets, tuple(unclaimed))


def rename_target(path: Path, *, old_stem: str, new_stem: str) -> Path:
    """Where a single sidecar goes when ``old_stem`` becomes ``new_stem``.

    Refuses a path that does not start with the old stem: a rename that
    guessed would put a file under a name that belongs to something else.
    """
    if not _claims(old_stem, path.name):
        raise ValueError(f"{path.name!r} does not belong to {old_stem!r}")
    return path.with_name(new_stem + path.name[len(old_stem):])


def planned_renames(
    video: str | os.PathLike[str],
    new_video: str | os.PathLike[str],
    *,
    listing: FolderListing | None = None,
) -> list[tuple[Path, Path]]:
    """The video and every sidecar, each paired with where it goes.

    Nothing is renamed. The list is what a rename, move or parking step
    carries out, and what its dry run prints.
    """
    return sidecars_of(video, listing=listing).renames(Path(new_video))


def iter_sets(folders: Iterable[str | os.PathLike[str]]) -> Iterator[SidecarSet]:
    """Every video's set in each folder, one listing per folder."""
    for folder in folders:
        yield from sidecars_in_folder(folder).sets.values()
