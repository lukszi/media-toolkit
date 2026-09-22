"""jfkit.libopts -- library options, including the ones the file omits.

The options document leaves out every field that still has its constructor
default, so reading it and writing it back silently changes behaviour unless
the defaults table is applied first. That table is data here, with a test.

Also here: repointing library roots after the data directory moves. The
virtual-folder records keep absolute paths, and when they go stale the
library looks empty rather than broken, which sends you looking in the wrong
place entirely.

One option deserves its own note: letting a container's embedded title win
over the catalogued name rewrites names wholesale on the next refresh.

Planned public API:
    read_options(config, library_id) -> LibraryOptions
    write_options(config, library_id, options, *, dry_run=True)
    repoint_roots(config, mapping, *, dry_run=True) -> RepointReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
