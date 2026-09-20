"""dubalign.cli -- sub-commands over the measurement and splice pipeline.

decode, probe, map, drift, changepoints, plan, splice, encode, clips,
verify, controls, make-fixtures.

Everything the old scripts hardcoded -- sources, tool paths, output names,
the hand-found segment boundaries -- is an argument or measured output here.

Planned public API:
    main(argv: list[str] | None = None) -> int

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
