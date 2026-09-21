"""mkvkit.langid.priors -- context that is evidence, kept separate from the audio.

Two priors, deliberately split because half of this is not server-shaped:

  release_token_prior(filename) reads language hints out of a filename. It is
  pure parsing, useful on its own, and testable with a table of invented
  names.

  sibling_prior(siblings) uses what the other tracks in the same file already
  say. Tracks are chosen by a person to complement each other, so their
  languages are not independent draws.

A trimmed mean over windows handles the case where one window catches a
foreign-language insert in an otherwise consistent track.

Planned public API:
    release_token_prior(filename: str) -> dict[str, float]
    sibling_prior(siblings) -> dict[str, float]
    trimmed_mean(values, frac) -> float

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
