"""jfkit.safedelete.evidence -- the half of a deletion that is measurement.

Nothing here deletes anything. It answers the four questions a deletion has
to answer first, and it answers them against the filesystem rather than
against a catalogue that may be out of date.

**Is this file the same file as that one?** Size first, because a difference
in size is an answer and costs nothing; then a hash of both, read once each.
A hash comparison that starts by reading two files that are different sizes
is a slow way to learn something that was free.

**Is the path still where the catalogue thinks it is?** Folders get renamed.
A candidate whose path does not exist is usually not a missing file, it is a
moved one, and deleting "the missing one" out of the catalogue loses the item.
The remapper applies a list of rewrites and reports which one matched, so a
guess is visible rather than silent.

**What else is in that folder?** A media file is rarely alone: subtitles,
artwork, a description file, sometimes another film nobody remembered. The
walk reports every file beside a candidate, split into what would go with it
and what would not, and a folder that turns out to hold something unrelated
is the clearest possible reason to stop.

**What is actually in the file?** A probe of every candidate, kept whole.
The comparison people skip is the one that matters: a smaller file with a
track the bigger one does not have is not the worse copy.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mkvkit.walk import walk

__all__ = [
    "LOOSE_TRACK_SUFFIXES",
    "MEDIA_SUFFIXES",
    "SIDECAR_SUFFIXES",
    "FolderContents",
    "Identity",
    "Remap",
    "folder_contents",
    "identical",
    "loose_tracks",
    "media_free",
    "remap_path",
    "sha256_of",
]

log = logging.getLogger(__name__)

#: How much is read at a time when hashing. Large enough that the syscall
#: overhead disappears, small enough to stay out of the way of everything else.
CHUNK = 16 << 20

#: What counts as media. A folder holding one of these is not media-free.
MEDIA_SUFFIXES = frozenset({
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg", ".m2ts",
    ".ts", ".vob", ".iso", ".divx", ".flv", ".webm",
})

#: A track that lives beside the media instead of inside it: an external
#: audio track or a subtitle. A folder holding one is not "media-free" in any
#: sense that matters -- it is the only copy of that track.
LOOSE_TRACK_SUFFIXES = frozenset({
    ".mka", ".ac3", ".eac3", ".dts", ".dtshd", ".thd", ".truehd", ".flac",
    ".aac", ".m4a", ".mp3", ".opus", ".ogg", ".wav", ".mp2",
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi",
})

#: Files that belong to a media file and follow it wherever it goes.
SIDECAR_SUFFIXES = frozenset({
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi",
    ".nfo", ".jpg", ".jpeg", ".png", ".txt",
})


@dataclass(frozen=True)
class Identity:
    """Whether two files are the same bytes, and what was measured to say so."""

    same: bool
    reason: str
    left_size: int = 0
    right_size: int = 0
    digest: str | None = None

    def __str__(self) -> str:
        return f"{'identical' if self.same else 'different'}: {self.reason}"


@dataclass(frozen=True)
class Remap:
    """A path that was not where it was expected, and where it turned out to be."""

    original: Path
    found: Path | None
    rule: str | None = None

    @property
    def moved(self) -> bool:
        return self.found is not None and self.found != self.original


@dataclass(frozen=True)
class FolderContents:
    """Everything beside a candidate, split into what goes with it and what does not."""

    folder: Path
    belongs: tuple[Path, ...] = ()
    other_media: tuple[Path, ...] = ()
    unrelated: tuple[Path, ...] = ()
    total_bytes: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def safe_to_remove_folder(self) -> bool:
        """A folder is only removable when nothing unaccounted for is in it."""
        return not self.other_media and not self.unrelated

    def __str__(self) -> str:
        return (
            f"{self.folder}: {len(self.belongs)} file(s) belong to this item, "
            f"{len(self.other_media)} other media file(s), "
            f"{len(self.unrelated)} other file(s)"
        )


def sha256_of(path: Path | str, *, chunk: int = CHUNK) -> str:
    """The digest of a file, read once, sequentially."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def identical(left: Path | str, right: Path | str, *, chunk: int = CHUNK) -> Identity:
    """Whether two files are byte for byte the same. Cheap answer first."""
    a, b = Path(left), Path(right)
    if not a.is_file() or not b.is_file():
        return Identity(False, "one of them is not there")
    size_a, size_b = a.stat().st_size, b.stat().st_size
    if size_a != size_b:
        return Identity(False, "the sizes differ", size_a, size_b)
    digest_a = sha256_of(a, chunk=chunk)
    digest_b = sha256_of(b, chunk=chunk)
    if digest_a != digest_b:
        return Identity(False, "the same size, different contents", size_a, size_b)
    return Identity(True, "the same size and the same digest", size_a, size_b, digest_a)


def remap_path(
    path: Path | str, rules: Sequence[tuple[str, str]] = ()
) -> Remap:
    """Where a path that is not there has probably gone.

    A candidate whose path does not exist is usually a folder that was
    renamed, not a file that was removed. The rules are pairs of substrings,
    applied in order, and the first that produces something that exists wins.
    The rule that matched is reported so the guess is not silent.
    """
    here = Path(path)
    if here.exists():
        return Remap(here, here, None)
    for old, new in rules:
        candidate = Path(str(here).replace(old, new))
        if candidate != here and candidate.exists():
            log.info("remapped a stale path by rule %r -> %r", old, new)
            return Remap(here, candidate, f"{old} -> {new}")
    return Remap(here, None, None)


def folder_contents(
    media: Path | str,
    *,
    media_suffixes: Iterable[str] = MEDIA_SUFFIXES,
    sidecar_suffixes: Iterable[str] = SIDECAR_SUFFIXES,
    also_belonging: Mapping[str, Sequence[Path]] | None = None,
) -> FolderContents:
    """Everything in the folder holding this file, sorted into three piles.

    A sidecar whose name starts with the media file's own name belongs to it.
    Anything else that is media is another item, and anything else at all is
    unaccounted for -- which is the interesting pile, because it is the one
    that stops a folder being removed.
    """
    here = Path(media)
    folder = here.parent
    stem = here.stem.casefold()
    media_kinds = {s.lower() for s in media_suffixes}
    sidecar_kinds = {s.lower() for s in sidecar_suffixes}

    belongs: list[Path] = [here] if here.is_file() else []
    other_media: list[Path] = []
    unrelated: list[Path] = []
    total = 0
    if not folder.is_dir():
        return FolderContents(folder, notes=("the folder is not there",))

    for entry in sorted(folder.rglob("*")):
        if not entry.is_file() or entry == here:
            continue
        total += entry.stat().st_size
        suffix = entry.suffix.lower()
        if suffix in sidecar_kinds and entry.stem.casefold().startswith(stem[:40]):
            belongs.append(entry)
        elif suffix in media_kinds:
            other_media.append(entry)
        else:
            unrelated.append(entry)

    extra = (also_belonging or {}).get(str(here), ())
    belongs += [Path(p) for p in extra]
    notes: list[str] = []
    if other_media:
        notes.append(
            "another media file is in this folder: removing the folder would take "
            "it too, and it belongs to a different item"
        )
    return FolderContents(
        folder=folder,
        belongs=tuple(belongs),
        other_media=tuple(other_media),
        unrelated=tuple(unrelated),
        total_bytes=total,
        notes=tuple(notes),
    )


def media_free(
    folder: Path | str, *, media_suffixes: Iterable[str] = MEDIA_SUFFIXES
) -> bool:
    """True when nothing below this folder is a media file, and nothing is hidden.

    A symbolic link, a junction or a folder that cannot be listed makes the
    answer False: the walk does not follow them (:mod:`mkvkit.walk`), so it
    cannot vouch for what is behind them.

    The one category of folder that can be removed without a per-file
    argument: a release folder left behind with nothing in it but artwork and
    description files.
    """
    kinds = {s.lower() for s in media_suffixes}
    here = Path(folder)
    if not here.is_dir():
        return False
    tree = walk(here, sizes=False)
    if any(entry.path.suffix.lower() in kinds for entry in tree):
        return False
    # A link, a junction or an unreadable folder below it is somewhere this
    # check did not look, so nothing can be said about what is there -- and
    # removing the folder would take the link with it.
    return not tree.skipped


def loose_tracks(folder: Path | str) -> list[Path]:
    """Every external audio or subtitle file below a folder.

    Reported by the media-free check as a refusal of its own: a folder with
    no video in it can still hold the only copy of a track.
    """
    here = Path(folder)
    if not here.is_dir():
        return []
    return sorted(
        entry.path for entry in walk(here, suffixes=LOOSE_TRACK_SUFFIXES, sizes=False)
    )

