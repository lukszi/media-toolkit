"""mkvkit.devices -- which volume backs a path, resolved rather than parsed.

The file side and the server side both have to answer "are these two paths on
the same disk" before they let two heavy readers loose, and the answer has to
be the same in both. It lives here, in the package the other one depends on,
and :mod:`jfkit.devices` re-exports it.

**Never key a decision on a string somebody else formatted.** A path is
resolved to its mount point or volume root first, and that is what is
compared. The first version of this compared the strings another program
printed; they carried a protocol prefix on one platform and not another,
paths landed in the wrong lane, and two readers ran on one disk.

**A volume is not always a disk.** Two partitions of one spinning disk are two
volumes and one disk. Nothing here can see that without a privileged call, so
every function that groups work by device takes the grouping function as a
parameter: a caller who knows that two volumes share a spindle passes one that
says so (see :func:`mkvkit.lanes.map_by_device`).
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["Device", "device_of", "same_device"]

#: A device is named by its mount point or volume root, normalised. It is a
#: string rather than a number because the number is not stable across
#: platforms and the mount point is what a person recognises.
Device = str


def device_of(path: Path | str) -> Device:
    """The mount point or volume root that backs this path.

    Resolved rather than parsed: a relative path, a link, or a path that does
    not exist yet all answer with the device the write would land on.
    """
    here = Path(path).expanduser()
    try:
        here = here.resolve()
    except OSError:  # pragma: no cover - a path that cannot be resolved at all
        here = here.absolute()

    if os.name == "nt":
        anchor = here.anchor
        return anchor.rstrip("\\/").upper() or str(here)

    candidate = here
    while not os.path.ismount(candidate) and candidate != candidate.parent:
        candidate = candidate.parent
    return str(candidate)


def same_device(a: Path | str, b: Path | str) -> bool:
    """Whether two paths would be served by the same disk."""
    return device_of(a) == device_of(b)
