"""dubalign.plan -- the splice plan as reviewable data.

The design idea worth publishing: a plan is a TOML document, not a list of
constants in a script. It can be read, diffed, hand-edited and re-run, and a
seam that was placed wrongly can be moved by editing one number.

A seam is placed at the quietest instant on BOTH sides within a bracket
around the detected change, not at the mathematically exact point, and the
crossfade is equal-power over a few milliseconds rather than a long ramp.

Planned public API:
    @dataclass(frozen=True) Seam / Segment / SplicePlan
    SplicePlan.to_toml() / SplicePlan.from_toml(text)
    plan_from_measurements(dense, drift, changepoints, *, quiet_search=True)

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
