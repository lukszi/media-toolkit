"""dubalign.controls -- the known-answer harness, run before anything is trusted.

Three controls, all synthetic, all fast:

  1. a track measured against itself must read 0.000,
  2. a pair built with a known offset must be recovered as that offset,
  3. the same pair measured at two different starting points must give the
     same answer.

A measurement pipeline that has not passed these is not evidence. The second
control in particular catches the leading-gap error that makes a naive
correlation confidently wrong.

Planned public API:
    controls(tools, tmp) -> ControlReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
