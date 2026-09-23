"""mkvkit.chapters.transcripts -- turn a transcript into evidence you can align.

A transcriber emits long segments; a long segment straddling a mark attributes
its words to both sides of the mark and blunts every alignment score that uses
it. Re-cutting the output into short cues from the word timestamps fixes that,
and cues that straddle a mark are trimmed to the side they belong to.

Rebuilding a window set is idempotent: running it twice produces the same
bytes, and an interrupted run leaves no half-written file behind, because the
job that consumes these runs for hours and WILL be interrupted.

Planned public API:
    cues_from_words(segments) -> list[Cue]
    trim_to_marks(cues, marks) -> list[Cue]
    write_windows(path, windows) -> None   # atomic, idempotent

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
