"""dubalign.boundary -- locate a cut by running two correlations at once.

A local correlation is computed at the offset before the change and at the
offset after it; the point where they cross is the cut, to a few tens of
milliseconds. Far cheaper and far more stable than searching for the offset
again at every position.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
