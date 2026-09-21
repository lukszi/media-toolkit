"""mkvkit.propedit -- one safe header edit, replacing separate copies.

Read the track table before and after, diff track count, ids, codec ids,
channel counts, the default/forced/enabled flags and the track names, and
write both a rollback table and an applied table before returning.

It refuses a file whose container is not actually Matroska. The header editor
silently does nothing there while the muxer happily parses the file, so the
command exits 0 and nothing changed -- which is why 'it exited 0' is not
verification. Such a file is routed to a remux instead.

Planned public API:
    safe_propedit(path, edits, *, must_not_change=DEFAULT_FIELDS, dry_run=True)
    class PropeditResult: rollback / applied / problems

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
