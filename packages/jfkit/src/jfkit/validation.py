"""jfkit.validation -- say beforehand when a path change costs a whole-library pass.

The server watches its library folders. A change inside a film's or a
series' folder is handled where it happened: the item whose folder it is gets
looked at again, and that is all. A change *one level up* is not. When a
folder directly under a library root appears or disappears, the item it
belonged to (or the new one) hangs off the library's own collection folder,
and the watcher's way of finding "the item affected by this change" walks up
to that collection folder and refreshes it -- which is a validation of the
whole library: every folder below the root listed again, and files it had
trouble with probed again. On a large library on a spinning disk that is
minutes to hours of reading nobody scheduled.

Nothing can avoid it; a person can choose *when* it happens. So every verb
that removes, parks, moves or renames something in a library should ask
:func:`expect_library_validation` first and print what it says -- in the dry
run too, where it is most useful -- so the change can be put on a quiet disk
at a quiet hour.

**Who calls it.** It is deliberately a plain function of paths and roots,
with no server call of its own, so it fits wherever a plan is built:

* ``jfkit delete`` -- a folder candidate (a media-free folder, or a leftover
  category that parks a whole release folder) is passed as ``removes``, and
  the warning is a note on the outcome and in the audit;
* ``jfkit dedupe`` and ``jfkit leftovers sweep`` -- every release folder the
  plan parks is passed as ``removes``, and the warning is a plan note;
* ``jfkit rename`` -- a renamed folder as ``removes``, every target as
  ``creates``; a target whose top-level folder already exists costs nothing
  and is not reported.
* ``jfkit notify`` already refuses to name a library root; this is the
  broader warning for the change itself, and is independent of whether
  anybody notifies.

The roots come from :func:`jfkit.refresh.library_roots`, which reads every
library's folders from the server; ``None`` there means they could not be
read, and :func:`warnings_for` then says so rather than saying nothing.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

__all__ = [
    "LibraryValidation",
    "expect_library_validation",
    "warnings_for",
]

log = logging.getLogger(__name__)


def _key(path: PurePath | str) -> tuple[str, ...]:
    """A path as comparable parts: either separator, without regard to case."""
    text = str(path).replace("\\", "/").rstrip("/")
    return tuple(part.casefold() for part in text.split("/") if part)


@dataclass(frozen=True)
class LibraryValidation:
    """One change that will make the server validate a whole library."""

    path: str
    root: str
    #: "removes", "creates" or "removes a root"
    change: str

    def __str__(self) -> str:
        if self.change == "removes a root":
            return (
                f"{self.path} is, or contains, the library folder {self.root}: removing it "
                "empties the library, and the server validates all of it"
            )
        name = PurePath(self.path.replace("\\", "/")).name
        return (
            f"this {self.change} {name!r}, a top-level folder of the library at "
            f"{self.root}: expect the server to validate that whole library (every "
            "folder below it listed again), so schedule it for a quiet disk"
        )


def expect_library_validation(
    *,
    roots: Iterable[str | PurePath],
    removes: Iterable[str | PurePath] = (),
    creates: Iterable[str | PurePath] = (),
    exists: Callable[[str], bool] | None = None,
) -> list[LibraryValidation]:
    """The changes among these that touch a library's top level.

    ``removes`` are paths that will stop existing (removed, parked, or the
    source of a move); a folder directly under a root, or a root itself, is
    reported. ``creates`` are paths that will start to exist (the destination
    of a move or a rename); one is reported when the top-level folder it
    lands in does not exist yet, which ``exists`` answers (the real file
    system by default).
    """
    present = exists or (lambda p: Path(p).exists())
    known = [(str(r), _key(r)) for r in roots]
    out: list[LibraryValidation] = []
    for raw in removes:
        key = _key(raw)
        for root, root_key in known:
            if not root_key:
                continue
            if key == root_key or root_key[:len(key)] == key:
                out.append(LibraryValidation(str(raw), root, "removes a root"))
            elif len(key) == len(root_key) + 1 and key[:len(root_key)] == root_key:
                out.append(LibraryValidation(str(raw), root, "removes"))
    for raw in creates:
        key = _key(raw)
        for root, root_key in known:
            if not root_key or len(key) <= len(root_key) or key[:len(root_key)] != root_key:
                continue
            text = str(raw).replace("\\", "/").rstrip("/")
            parts = [p for p in text.split("/") if p != ""]
            # the top-level folder, spelt as the caller spelt it
            top = ("/" if text.startswith("/") else "") + "/".join(parts[:len(root_key) + 1])
            if not present(top):
                out.append(LibraryValidation(top, root, "creates"))
    return _unique(out)


def _unique(found: Sequence[LibraryValidation]) -> list[LibraryValidation]:
    seen: set[tuple[tuple[str, ...], str, tuple[str, ...]]] = set()
    out: list[LibraryValidation] = []
    for warning in found:
        mark = (_key(warning.path), warning.change, _key(warning.root))
        if mark not in seen:
            seen.add(mark)
            out.append(warning)
    return out


def warnings_for(
    roots: Sequence[str] | None,
    *,
    removes: Iterable[str | PurePath] = (),
    creates: Iterable[str | PurePath] = (),
    exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Printable warnings; one of its own when the roots could not be read."""
    if roots is None:
        return [
            "the server's library folders could not be read, so whether this change "
            "costs a whole-library validation is unknown"
        ]
    return [
        str(w) for w in expect_library_validation(
            roots=roots, removes=removes, creates=creates, exists=exists,
        )
    ]
