"""dubalign.check_raw -- sanity checks on a raw decode before trusting it.

Length against the container's duration, silence at the head and tail, and a
level histogram. A decode that lost its first packets measures beautifully and
is wrong by exactly that much.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
