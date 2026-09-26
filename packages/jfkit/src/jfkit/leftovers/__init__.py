"""jfkit.leftovers -- what a library leaves behind, swept by category, and what it lacks.

``jfkit leftovers sweep`` walks the library folders (never through a link or
a junction), sorts every file and folder with the rules in
:mod:`jfkit.safedelete.junk`, and proposes four kinds of leftover:

* ``release-junk`` -- tracker notes, shortcuts, programs, torrent padding,
  release screenshots, checksum lists, and folders of nothing else;
* ``dead-release-folder`` -- a release folder with no video left, only
  description files, artwork and preview tiles, and nothing catalogued in it;
* ``sample`` -- a release sample, listed always and moved only when released;
* ``corrupt-unplayable`` -- a video named with ``--corrupt`` whose payload
  check failed and of which no other copy is catalogued.

Each proposal goes through the preconditions of :mod:`jfkit.safedelete`,
and what passes and was released becomes a plan (:mod:`mkvkit.steps`) that
parks -- never deletes -- and checks every candidate again as it runs.

``jfkit leftovers missing`` reports the opposite: rows with no file, files
the walk did not find, gaps in a season's numbering, and release folders
whose release is not in the catalogue at all. It only reads.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from .missing import Missing, fetch_virtual, find_missing
from .plan import Assessed, actions, assess, build_plan, park_target
from .scan import Finding, Kept, Scan, scan, scan_root

__all__ = [
    "Assessed",
    "Finding",
    "Kept",
    "Missing",
    "Scan",
    "actions",
    "assess",
    "build_plan",
    "fetch_virtual",
    "find_missing",
    "park_target",
    "scan",
    "scan_root",
]
