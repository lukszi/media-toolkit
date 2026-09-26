"""jfkit.safedelete.checks -- one precondition and its answer.

Its own module so the evidence modules can return checks without importing
the run that uses them.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Check"]


@dataclass(frozen=True)
class Check:
    """One precondition, its answer, and enough detail to argue with."""

    name: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'ok' if self.ok else 'FAIL'}: {self.name}" + (
            f" -- {self.detail}" if self.detail else ""
        )
