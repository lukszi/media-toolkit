"""mkvkit.walk -- walk a directory tree without wandering off it.

A plain ``os.walk(root, followlinks=False)`` is not safe on the platform most
libraries live on. An NTFS *junction* is a directory reparse point, and
``os.path.islink`` answers ``False`` for it -- so the walk descends into it,
and a read-only inventory of one disk quietly becomes an inventory of another
disk, one nobody meant to touch and nobody is coordinating reads on. The same
goes for a directory symbolic link on any platform once somebody passes
``followlinks=True`` to get past an unrelated problem.

This walker does three things differently.

**Links of every kind are reported and not followed.** A symbolic link (file
or directory), a junction, and any other *directory* reparse point is not
entered by default. Each one is recorded as :class:`Skipped` with its reason,
so a caller can say "two folders were left out, and here is why" instead of
leaving the reader to wonder what was covered. Following can be asked for
(``follow_links=True``); then every directory is identified by its device and
inode, and one already seen -- a link loop, or the same tree reached twice --
is skipped with :attr:`SkipReason.LOOP`.

A *file* whose reparse point is not a link (a deduplicated or cloud-backed
file, for instance) is still a file and is yielded as one: the tag is what
decides, not the attribute alone.

**Excludes are part of the walk.** ``exclude`` takes glob patterns matched
against both an entry's name and its path relative to the root (with ``/``
separators), and ``exclude_paths`` takes paths, compared after
normalisation. An excluded directory is not entered, and it is reported.

**Problems are reported, not raised.** A directory that cannot be listed --
permission denied, vanished half way through -- becomes a :class:`Skipped`
entry with the error's text, and the walk carries on. Only the root itself is
allowed to fail loudly: a walk of nothing is not a result.

**It costs one listing per directory and no more.** The walk is iterative and
built on ``os.scandir``. The kind of each entry comes from the listing; on
Windows the listing also carries the attributes, the reparse tag and the size,
so link detection and sizes cost nothing extra. Elsewhere a symbolic link is
known from the listing and a size costs one ``lstat`` per file, which
``sizes=False`` avoids. Entries are yielded in name order within each
directory, so two walks of one tree agree.

Matching is case-insensitive on Windows and case-sensitive elsewhere, as the
file systems are.

    >>> from mkvkit.walk import walk
    >>> tree = walk("/srv/media/series", exclude=["*.partial", "Extras"])
    >>> for entry in tree:  # doctest: +SKIP
    ...     print(entry.relative, entry.size)
    >>> for skipped in tree.skipped:  # doctest: +SKIP
    ...     print(skipped)
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import fnmatch
import logging
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

__all__ = [
    "Entry",
    "SkipReason",
    "Skipped",
    "Walk",
    "is_link_or_junction",
    "link_kind",
    "walk",
]

log = logging.getLogger(__name__)

#: The reparse tags that make a directory entry a link rather than data.
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a junction (or a volume mount point)
IO_REPARSE_TAG_SYMLINK = 0xA000000C

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_WINDOWS = os.name == "nt"


class SkipReason(Enum):
    """Why an entry was not yielded, or a directory not entered."""

    #: A symbolic link, file or directory.
    SYMLINK = "symlink"
    #: An NTFS junction or volume mount point.
    JUNCTION = "junction"
    #: A directory reparse point of some other kind.
    REPARSE_POINT = "reparse-point"
    #: Matched an ``exclude`` pattern or an ``exclude_paths`` entry.
    EXCLUDED = "excluded"
    #: A directory already walked, reached again through a followed link.
    LOOP = "loop"
    #: The directory could not be listed or the entry could not be read.
    ERROR = "error"


@dataclass(frozen=True)
class Skipped:
    """One thing the walk left out, and why."""

    path: Path
    reason: SkipReason
    detail: str = ""

    def __str__(self) -> str:
        tail = f" ({self.detail})" if self.detail else ""
        return f"skipped {self.reason.value}: {self.path}{tail}"


@dataclass(frozen=True)
class Entry:
    """One file, or one directory when those are asked for.

    ``size`` is in bytes for a file and ``None`` for a directory, or when
    sizes were not asked for, or when the entry could not be measured.
    ``link`` is set when the entry is a followed link (``follow_links=True``).
    """

    path: Path
    relative: PurePosixPath
    is_dir: bool
    size: int | None = None
    link: SkipReason | None = None


def _norm(text: str) -> str:
    return text.casefold() if _WINDOWS else text


def _norm_path(path: str | os.PathLike[str]) -> str:
    return _norm(os.path.normpath(os.path.abspath(os.fspath(path))))


def _classify_stat(st: os.stat_result, is_dir: bool) -> SkipReason | None:
    """The link kind an ``lstat``-style result describes, if it is one."""
    if stat.S_ISLNK(st.st_mode):
        return SkipReason.SYMLINK
    attributes = getattr(st, "st_file_attributes", 0)
    if not attributes & _REPARSE:
        return None
    tag = getattr(st, "st_reparse_tag", 0)
    if tag == IO_REPARSE_TAG_SYMLINK:
        return SkipReason.SYMLINK
    if tag == IO_REPARSE_TAG_MOUNT_POINT:
        return SkipReason.JUNCTION
    # Any other tag on a file is data stored differently (deduplicated,
    # cloud-backed); on a directory it is somewhere else, and not entered.
    return SkipReason.REPARSE_POINT if is_dir else None


def link_kind(path: str | os.PathLike[str]) -> SkipReason | None:
    """Whether a path is a symbolic link, a junction or a directory reparse point.

    ``None`` for an ordinary file or directory, and for a path that does not
    exist. This is the check ``os.path.islink`` does not make: it answers
    ``False`` for a junction.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    attributes = getattr(st, "st_file_attributes", 0)
    return _classify_stat(st, stat.S_ISDIR(st.st_mode) or bool(attributes & _DIRECTORY))


def is_link_or_junction(path: str | os.PathLike[str]) -> bool:
    """True for a symbolic link, a junction or any directory reparse point."""
    return link_kind(path) is not None


@dataclass
class Walk:
    """A walk of one tree: iterate it for the entries, then read ``skipped``.

    The walk runs as it is iterated, once; iterating a second time walks the
    tree again and replaces ``skipped``.
    """

    root: Path
    exclude: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    follow_links: bool = False
    include_dirs: bool = False
    suffixes: frozenset[str] | None = None
    sizes: bool = True
    on_skip: Callable[[Skipped], None] | None = None
    skipped: list[Skipped] = field(default_factory=list)
    _patterns: tuple[str, ...] = field(default=(), init=False, repr=False)
    _exclude_paths: frozenset[str] = field(default=frozenset(), init=False, repr=False)

    def __iter__(self) -> Iterator[Entry]:
        self.skipped = []
        return self._walk()

    # ------------------------------------------------------------------ helpers
    def _skip(self, path: str, reason: SkipReason, detail: str = "") -> None:
        found = Skipped(Path(path), reason, detail)
        self.skipped.append(found)
        log.debug("%s", found)
        if self.on_skip is not None:
            self.on_skip(found)

    def _excluded(self, path: str, name: str, relative: str) -> bool:
        if self._exclude_paths and _norm_path(path) in self._exclude_paths:
            return True
        folded_name, folded_rel = _norm(name), _norm(relative)
        return any(
            fnmatch.fnmatchcase(folded_name, pattern)
            or fnmatch.fnmatchcase(folded_rel, pattern)
            for pattern in self._patterns
        )

    def _identity(self, path: str) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    # --------------------------------------------------------------------- walk
    def _walk(self) -> Iterator[Entry]:
        self._patterns = tuple(_norm(p) for p in self.exclude)
        self._exclude_paths = frozenset(_norm_path(p) for p in self.exclude_paths)
        suffixes = (
            None if self.suffixes is None
            else frozenset(s.lower() for s in self.suffixes)
        )
        root = os.fspath(self.root)
        # The root has to exist and be a directory; anything else is not a walk.
        if not os.path.isdir(root):
            raise NotADirectoryError(f"not a directory: {root}")

        seen: set[tuple[int, int]] = set()
        if self.follow_links:
            identity = self._identity(root)
            if identity is not None:
                seen.add(identity)

        stack: list[tuple[str, str]] = [(root, "")]
        while stack:
            directory, relative_dir = stack.pop()
            try:
                with os.scandir(directory) as listing:
                    entries = sorted(listing, key=lambda e: e.name)
            except OSError as exc:
                self._skip(directory, SkipReason.ERROR, exc.strerror or str(exc))
                continue

            subdirectories: list[tuple[str, str]] = []
            for entry in entries:
                relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    link = self._link_of(entry, is_dir)
                except OSError as exc:
                    self._skip(entry.path, SkipReason.ERROR, exc.strerror or str(exc))
                    continue

                if self._excluded(entry.path, entry.name, relative):
                    self._skip(entry.path, SkipReason.EXCLUDED)
                    continue

                if link is not None:
                    if not self.follow_links:
                        self._skip(entry.path, link)
                        continue
                    # followed: what it points at decides what it is
                    try:
                        is_dir = entry.is_dir(follow_symlinks=True)
                    except OSError as exc:
                        self._skip(entry.path, SkipReason.ERROR, exc.strerror or str(exc))
                        continue
                    if not is_dir and not os.path.exists(entry.path):
                        self._skip(entry.path, SkipReason.ERROR, "the link's target is missing")
                        continue

                if is_dir:
                    if self.follow_links:
                        identity = self._identity(entry.path)
                        if identity is not None and identity in seen:
                            self._skip(entry.path, SkipReason.LOOP)
                            continue
                        if identity is not None:
                            seen.add(identity)
                    if self.include_dirs:
                        yield Entry(Path(entry.path), PurePosixPath(relative), True,
                                    None, link)
                    subdirectories.append((entry.path, relative))
                    continue

                if suffixes is not None and \
                        os.path.splitext(entry.name)[1].lower() not in suffixes:
                    continue
                yield Entry(Path(entry.path), PurePosixPath(relative), False,
                            self._size_of(entry, link), link)

            # reversed, so the stack pops them in name order
            stack.extend(reversed(subdirectories))

    def _link_of(self, entry: os.DirEntry[str], is_dir: bool) -> SkipReason | None:
        if entry.is_symlink():
            return SkipReason.SYMLINK
        if not _WINDOWS:
            return None
        # On Windows this stat comes from the directory listing: no extra call.
        return _classify_stat(entry.stat(follow_symlinks=False), is_dir)

    def _size_of(self, entry: os.DirEntry[str], link: SkipReason | None) -> int | None:
        if not self.sizes:
            return None
        try:
            return entry.stat(follow_symlinks=link is not None).st_size
        except OSError:
            return None


def walk(
    root: str | os.PathLike[str],
    *,
    exclude: Iterable[str] = (),
    exclude_paths: Iterable[str | os.PathLike[str]] = (),
    follow_links: bool = False,
    include_dirs: bool = False,
    suffixes: Iterable[str] | None = None,
    sizes: bool = True,
    on_skip: Callable[[Skipped], None] | None = None,
) -> Walk:
    """Walk ``root`` without following links, and say what was left out.

    ``suffixes`` limits the files yielded to those extensions (with the dot,
    any case); directories are walked regardless. ``include_dirs`` yields
    directories too, before their contents. ``on_skip`` is called with each
    :class:`Skipped` as it happens, for a caller that reports as it goes.

    The returned :class:`Walk` is lazy: nothing is read until it is iterated.
    """
    return Walk(
        root=Path(root),
        exclude=tuple(exclude),
        exclude_paths=tuple(os.fspath(p) for p in exclude_paths),
        follow_links=follow_links,
        include_dirs=include_dirs,
        suffixes=None if suffixes is None else frozenset(suffixes),
        sizes=sizes,
        on_skip=on_skip,
    )
