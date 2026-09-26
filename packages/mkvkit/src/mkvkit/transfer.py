"""mkvkit.transfer -- copy or move a file, and prove the copy before relying on it.

Moving a media file between volumes is a copy followed by a delete, and the
delete is the dangerous half: it is safe only if the copy is known to be the
same bytes. A copy that is renamed into place while it is still being written
is also a problem of its own -- a media server watching the folder sees a
half-written file and catalogues it.

So :func:`verified_copy` does it in this order, and stops at the first thing
that is not right:

1. **Refuse to overwrite.** A destination that exists is never replaced.
2. **Copy to a partial file** beside the destination (or in ``stage``, a folder
   on the same volume outside anything that is watched), hashing the source
   as it is read, and flush it to the device.
3. **Hash the partial file from the destination side** and compare. A copy
   is proved by reading what arrived, not by trusting what was written.
4. **Rename it into place**, without replacing anything that appeared in the
   meantime. On one volume a rename is instant, so nothing ever sees a
   half-written file under the final name.
5. **Only then, and only for a move, remove the source.**

A failure at any step removes the partial file and leaves the source alone.

**The operating system's cache.** The read in step 3 comes straight after
the write, and on most systems it is served from memory, not from the
device: it proves the data that was handed to the device, not that the
device stored it. Where that difference matters -- a disk that is suspect,
a network share -- read the file again later, after the cache has moved on
(``mkvkit integrity``, or this module's :func:`digest_of`), before removing
anything else that depends on it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["CHUNK", "CopyReport", "digest_of", "verified_copy"]

log = logging.getLogger(__name__)

#: How much is read and written at a time.
CHUNK = 16 << 20

#: Said in every report whose proof came from a read straight after the write.
CACHE_NOTE = (
    "the destination was hashed straight after it was written, which the "
    "operating system usually answers from its cache: this proves what was "
    "handed to the device; read it again later to prove what the device kept"
)


@dataclass(frozen=True)
class CopyReport:
    """What was asked, what was done, and the evidence for it."""

    source: Path
    destination: Path
    move: bool = False
    applied: bool = False
    size: int | None = None
    algorithm: str = "sha256"
    source_digest: str | None = None
    destination_digest: str | None = None
    source_removed: bool = False
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def verified(self) -> bool:
        return (
            self.source_digest is not None
            and self.source_digest == self.destination_digest
        )

    def __str__(self) -> str:
        verb = "move" if self.move else "copy"
        if self.problems:
            state = "REFUSED" if not self.applied else "FAILED"
        elif not self.applied:
            state = "would " + verb
        else:
            state = ("moved" if self.source_removed else "copied") + ", verified"
        lines = [f"{self.source} -> {self.destination}: {state}"]
        if self.size is not None:
            lines.append(f"  {self.size} bytes")
        if self.source_digest:
            lines.append(f"  {self.algorithm} source      {self.source_digest}")
        if self.destination_digest:
            lines.append(f"  {self.algorithm} destination {self.destination_digest}")
        lines += [f"  problem: {p}" for p in self.problems]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def digest_of(path: Path | str, algorithm: str = "sha256") -> str:
    """One sequential read of a file, hashed."""
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK):
            hasher.update(block)
    return hasher.hexdigest()


def _same_volume(one: Path, other: Path) -> bool:
    return os.stat(one).st_dev == os.stat(other).st_dev


def _place(partial: Path, destination: Path) -> None:
    """Rename into place without replacing anything that is there.

    A hard link fails when the name exists, which is the no-overwrite rename
    every platform lacks; where links are not supported, a check immediately
    before a plain rename is the best available.
    """
    try:
        os.link(partial, destination)
    except FileExistsError:
        raise
    except OSError:
        if destination.exists():
            raise FileExistsError(str(destination)) from None
        os.rename(partial, destination)
        return
    partial.unlink()


def verified_copy(
    source: Path | str,
    destination: Path | str,
    *,
    move: bool = False,
    stage: Path | str | None = None,
    dry_run: bool = True,
    algorithm: str = "sha256",
) -> CopyReport:
    """Copy (or move) one file and prove the copy by reading it back.

    ``destination`` may be a folder, in which case the file keeps its name.
    ``stage`` is where the partial file is written; it must be on the same
    volume as the destination, so the final step is a rename. Nothing is
    written in a dry run, and every refusal is decided before the first byte
    is copied.
    """
    src = Path(source)
    dest = Path(destination)
    if dest.is_dir():
        dest = dest / src.name
    base = CopyReport(source=src, destination=dest, move=move, algorithm=algorithm)
    problems: list[str] = []

    if not src.is_file():
        problems.append(f"the source is not a file: {src}")
    if dest.exists():
        problems.append(f"the destination exists and is never replaced: {dest}")
    if not dest.parent.is_dir():
        problems.append(f"the destination folder does not exist: {dest.parent}")
    staging = Path(stage) if stage is not None else dest.parent
    if stage is not None and not staging.is_dir():
        problems.append(f"the staging folder does not exist: {staging}")
    if algorithm not in hashlib.algorithms_available:
        problems.append(f"{algorithm}: not a hash this system has")
    if problems:
        return _with(base, problems=problems)

    size = src.stat().st_size
    if stage is not None and not _same_volume(staging, dest.parent):
        problems.append(
            "the staging folder is on another volume than the destination, so "
            "the last step would be a second copy rather than a rename"
        )
    free = shutil.disk_usage(staging).free
    if size > free:
        problems.append(f"{size} bytes do not fit in the {free} free at {staging}")
    if problems:
        return _with(base, size=size, problems=problems)
    if dry_run:
        return _with(base, size=size, notes=(
            "dry run: nothing was written",
            f"the partial file would be written in {staging} and renamed into place",
        ))

    partial = staging / f".{dest.name}.part-{uuid.uuid4().hex[:8]}"
    try:
        source_digest = _copy_hashing(src, partial, algorithm)
        shutil.copystat(src, partial)
        destination_digest = digest_of(partial, algorithm)
        if destination_digest != source_digest:
            raise _Mismatch(source_digest, destination_digest)
        _place(partial, dest)
    except _Mismatch as mismatch:
        _discard(partial)
        return _with(base, size=size, applied=True,
                     source_digest=mismatch.source, destination_digest=mismatch.arrived,
                     problems=("the copy does not hash like the source; it was "
                               "removed and the source is untouched",))
    except BaseException as exc:
        _discard(partial)
        if isinstance(exc, Exception):
            return _with(base, size=size, applied=True, problems=(
                f"the copy failed and was removed; the source is untouched: {exc}",
            ))
        raise

    removed = False
    notes = [CACHE_NOTE]
    if move:
        # the copy is in place and proved; only now is the source expendable
        if dest.stat().st_size != size:  # pragma: no cover - guarded by the hash
            return _with(base, size=size, applied=True, problems=(
                "the placed copy has the wrong size; the source was kept",
            ))
        src.unlink()
        removed = True
    log.info("%s %s -> %s, %s %s", "moved" if removed else "copied", src, dest,
             algorithm, source_digest)
    return _with(
        base, size=size, applied=True, source_digest=source_digest,
        destination_digest=destination_digest, source_removed=removed,
        notes=tuple(notes),
    )


class _Mismatch(Exception):
    def __init__(self, source: str, arrived: str) -> None:
        super().__init__("digest mismatch")
        self.source = source
        self.arrived = arrived


def _copy_hashing(src: Path, partial: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with open(src, "rb") as reader, open(partial, "xb") as writer:
        while block := reader.read(CHUNK):
            hasher.update(block)
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    return hasher.hexdigest()


def _discard(partial: Path) -> None:
    try:
        partial.unlink()
    except FileNotFoundError:
        pass


def _with(base: CopyReport, **changes: Any) -> CopyReport:
    for key in ("problems", "notes"):
        if key in changes:
            changes[key] = tuple(changes[key])
    return dataclasses.replace(base, **changes)
