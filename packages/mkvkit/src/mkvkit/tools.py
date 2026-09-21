"""mkvkit.tools -- find the external programs, once, and say where they came from.

Resolution order: the config entry, then an environment variable, then the
PATH, then a per-platform fallback list. Every resolution is logged once with
the version string the program reports, so a bug report says which build was
used. A missing program raises an error naming every location that was tried.

No drive letter, no install directory and no shell wrapper appears anywhere
in the library; a location somebody names is configuration.

Planned public API:
    find_tool(name: str) -> Path
    tool_version(path: Path) -> str

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
