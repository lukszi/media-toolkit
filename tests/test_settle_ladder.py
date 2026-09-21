"""The settle ladder, tested with no model, no GPU and no audio.

Hand-written probability vectors exercise aggregation, the margins, the
agreement fraction, the priors, the trimmed mean, the mixed verdict and the
no-dialogue verdict. This is the highest-value test in the project: the part
most likely to be subtly wrong is the part that needs no media to test.

Planned public API:
    test_confirm_needs_agreement()
    test_overturn_bar_is_asymmetric()

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
