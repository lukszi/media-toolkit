"""mkvkit.tags -- Matroska tag elements, and why they beat the header.

A language written into a tag element overrides the track header for every
reader that looks at tags, including the players and servers that matter. A
tool that fixes only the header therefore leaves the file still wrong, and
the wrongness is invisible in the track table.

So a language change is two edits, or it is not a language change.

Planned public API:
    read_tags(path) -> Tags
    set_track_language(path, track_uid, language, *, dry_run=True)

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
