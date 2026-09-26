"""jfkit.rename -- rename videos or folders in a library as one audited plan.

A rename in a media library is not one file operation. The server reads the
new name with its own parser and may file the video somewhere else; the
sidecars have to follow the video, except the metadata document, which carries
the old episode's data and would be read back in; the item gets a new
identifier and every user's watched state stays on the old one; the server has
to be told which folders changed without being made to walk a whole library;
and afterwards somebody has to look at what the server made of it.

This package does all of that in one shape:

1. **Predict** (:mod:`.plan`). Every target is read with the naming port
   (:mod:`jfkit.naming`) and compared with what was intended -- an episode
   number, an extra, or no number at all -- and checked for length (with the
   preview tiles the server writes beside it) and for collisions, among the
   targets and with what is already on disk. One mismatch refuses the plan.
2. **Carry** each video's sidecars with it (:mod:`mkvkit.sidecars`) and
   **park** a stale ``.nfo`` in a folder outside the library instead.
3. **Snapshot** every user's watched state for the whole scope
   (:mod:`jfkit.userdata`) before anything moves.
4. **Apply** the renames as :mod:`mkvkit.steps` actions: never replacing
   anything, cycles and chains through temporary names, audited, resumable.
5. **Notify** the deepest changed folders only, refusing a library root and a
   top-level folder the server does not know.
6. **Wait** for the new items, bounded; refresh them without replacing when
   asked.
7. **Replay** the watched state onto the new identifiers, clearing rows the
   server handed out by slot, and verify it.
8. **Verify** the end state and print a report.

The dry run is the default; ``--apply`` executes; the audit is JSON lines.
:mod:`.server` holds the server side and :mod:`.verb` the ``jfkit rename``
sub-command.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from .plan import (
    Expect,
    FilePlan,
    Options,
    Pair,
    Problem,
    Video,
    infer_expect,
    parse_expect,
    plan_files,
    read_mapping,
)

__all__ = [
    "Expect",
    "FilePlan",
    "Options",
    "Pair",
    "Problem",
    "Video",
    "infer_expect",
    "parse_expect",
    "plan_files",
    "read_mapping",
]
