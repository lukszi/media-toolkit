"""dubalign.drift -- fine offset measurement inside one constant stretch.

A waveform-level search around a piecewise base offset. Within a stretch the
residual is a slope, and that slope is the transfer's rate difference.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
