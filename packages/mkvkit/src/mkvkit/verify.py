"""mkvkit.verify -- prove the rebuilt file is the original plus the intended change.

Evidence is collected on BOTH sides and compared, and it includes the ordered
per-stream hashes of the packet payloads, not just the track table. Anything
less proves that a file exists, not that it is correct.

The expected difference is pluggable -- tracks dropped, tracks appended, or a
header-only edit -- so one module replaces copies that had drifted apart.

Some differences are recorded as notes rather than failures, and the comments
explaining WHY are the publishable part:

  * a modern language subtag added while the legacy element is unchanged; the
    legacy element is what most readers use, so it is compared strictly, and
    two sides carrying DIFFERENT modern tags still fails;
  * regenerated track and chapter identifiers, which nothing outside the file
    references and which the muxer remaps for tags itself;
  * a container duration that shrank while every kept stream hash matched --
    the dropped track simply ran past the rest, which is what removing it
    should do.

A missing index in the output IS a failure: it makes seeking a full read.

After a header-only edit the stream hashes are not recomputed, because a
header editor cannot alter a packet payload. Knowing what cannot have changed
and skipping the measurement is part of the discipline, not a shortcut.

Planned public API:
    collect(path) -> Evidence
    compare(orig: Evidence, built: Evidence, delta: ExpectedDelta) -> Comparison
    reheader(path, evidence) -> Evidence
    class TracksDropped / TracksAppended / HeaderOnly

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
