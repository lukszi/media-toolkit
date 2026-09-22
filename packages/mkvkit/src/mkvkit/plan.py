"""mkvkit.plan -- decide what to rebuild from policy, and explain the churn.

A plan is produced from a policy plus a survey, never from a list somebody
typed. Re-running it on a changed collection produces a different plan, and
the interesting output is the DIFFERENCE: which files entered the plan since
the last run, which left, and why each one moved.

Without that explanation a policy-driven planner is not trustworthy across
reruns, because there is no way to tell a policy change from a bug.

Planned public API:
    build_plan(survey, policy) -> Plan
    explain_churn(previous: Plan, current: Plan) -> ChurnReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
