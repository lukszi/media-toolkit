"""mkvkit.langid.worker -- sample a track and ask a detector what it hears.

Windows are chosen by speech activity, not by a fixed grid, and the budget is
per track rather than per file, so a long track does not cost proportionally
more. Results append to a JSONL keyed by (path, stream index, model), so an
interrupted run resumes instead of starting again.

Two lessons are encoded here. A per-output duration must bind to the output
it belongs to, not to the command as a whole, or every window after the first
is silently wrong. And a container with no index makes a per-window seek a
full read, so when the first seek is slow the worker switches to one
sequential decode instead of N of them.

Planned public API:
    class Detector(Protocol): detect(pcm, sr) -> dict[str, float]
    speech_windows(src, stream, *, n, seconds, vad=True) -> list[Window]
    scan_track(src, stream, detector, windows) -> TrackEvidence

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
