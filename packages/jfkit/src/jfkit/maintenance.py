"""jfkit.maintenance -- database and trickplay upkeep, with the guards intact.

Ported from a folder of one-off scripts into one module
that uses the stdlib sqlite3 driver rather than an external binary.

The guards are the point, not the SQL: refuse while a scheduled task is
running, stop the service through the controller hook rather than assuming
it can, snapshot with VACUUM INTO and never touch the live database file,
apply, restart, then verify row counts against the snapshot.

Trickplay restore is additive: it never overwrites and never deletes. A
content-type change wipes trickplay for an item and every recursive child,
which is worth publishing WITH its mitigation rather than as a warning.

Planned public API:
    reindex(config, *, dry_run=True) -> MaintenanceReport
    snapshot(db: Path, out: Path) -> Path
    restore_trickplay(src, dst, *, dry_run=True) -> RestoreReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
