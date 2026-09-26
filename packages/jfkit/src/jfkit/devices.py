"""jfkit.devices -- which physical device backs this path.

The gate in :mod:`jfkit.jobs` only works if two paths on one disk resolve to
the same answer and two paths on different disks do not. That sounds trivial
and is where the first version of this went wrong.

**The first version compared the strings another program printed.** Those
strings carry a protocol prefix on some platforms and not on others, and they
are spelled with whichever separator the program that wrote them preferred.
Paths landed in the wrong lane, two heavy readers ran on one disk,
and the machine became unusable until one of them finished.

So a path is resolved to its mount point or volume root first, and that is
what is compared. That resolution lives in :mod:`mkvkit.devices`, so the file
side groups work by the same answer, and is re-exported here. The lesson
generalises past this module: never key a decision on a string somebody
else formatted.

Whether the device is a spinning disk is a different question and an honest
``None`` where it cannot be answered. It can be read on one platform and not
on the others, and guessing is worse than not knowing -- the gate treats an
unknown device as one that needs protecting, which is the safe direction.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path, PurePath

from mkvkit.devices import Device, device_of, same_device

__all__ = ["Device", "device_of", "is_rotational", "mentions_device", "same_device"]

log = logging.getLogger(__name__)

#: Where a platform records whether a block device spins. Absent elsewhere.
ROTATIONAL = "/sys/block/{name}/queue/rotational"


def is_rotational(device: Device) -> bool | None:
    """True for a spinning disk, False for solid state, ``None`` when unknown.

    Unknown is a real answer here and is returned rather than guessed. Only
    one platform exposes this without a privileged call, and a gate that
    assumes "not spinning" because it could not find out is a gate that lets
    two readers onto the one disk that could not take them.
    """
    if sys.platform != "linux":
        return None
    try:
        source = _source_device(device)
    except OSError:  # pragma: no cover - unreadable mount table
        return None
    if source is None:
        return None
    flag = Path(ROTATIONAL.format(name=source))
    if not flag.is_file():
        return None
    try:
        return flag.read_text(encoding="utf-8").strip() == "1"
    except OSError:  # pragma: no cover - a device that vanished mid-read
        return None


def _source_device(mount_point: str) -> str | None:
    """The block device name behind a mount point, on the platform that says."""
    table = Path("/proc/mounts")
    if not table.is_file():
        return None
    for line in table.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != mount_point:
            continue
        name = PurePath(parts[0]).name
        # a partition belongs to its whole disk, which is what carries the flag
        return re.sub(r"(?<=[a-z])\d+$", "", name) or None
    return None


def mentions_device(command: str, device: Device) -> bool:
    """Whether a command line reads or writes something on this device.

    The check is deliberately shaped rather than a substring test. A bare
    volume name appears inside other programs' arguments -- a protocol prefix,
    a label, a parameter that happens to contain the letter -- and a substring
    test on one machine made every job on one disk look like a reader on
    another, which is the failure this whole gate exists to prevent.
    """
    if not device:
        return False
    # The shape of the device decides, not the host: a gate may reason about a
    # path that came from somewhere else, and "which platform am I on" is the
    # wrong question to answer it with.
    if re.fullmatch(r"[A-Za-z]:", device):
        letter = device[0]
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(letter)}:[\\/]", command,
                         re.IGNORECASE) is not None
    prefix = device.rstrip("/")
    if not prefix:
        # the root device: anything absolute is on it unless it is on another
        return bool(re.search(r"(?<![\w.])/\w", command))
    return re.search(rf"(?<![\w.]){re.escape(prefix)}/", command) is not None
