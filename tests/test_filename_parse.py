"""The filename parser, tested against a table of invented names.

This module predicts how a media server will read a filename BEFORE you
rename anything, which makes it the cheapest way to avoid a rename that
silently renumbers a season.

It is a port of somebody else's parser, so without a fixture table it will
eventually be confidently wrong against a newer release. The table and a
pinned note saying which version it was checked against are release blockers,
not nice-to-haves.

Planned public API:
    test_season_episode_forms()
    test_double_episode_end_number()

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
