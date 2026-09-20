"""dubalign.verify_pcm -- measure the built result against the reference.

Measurement points are spread across the whole runtime, not concentrated at
the seams the builder chose. Checking your own working at the places you
already thought about is not verification.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
