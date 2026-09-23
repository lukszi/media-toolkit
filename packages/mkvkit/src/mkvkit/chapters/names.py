"""mkvkit.chapters.names -- windows, mechanical rules, and no namer.

What ships: the window builder, the rule checks, and the grading pass.

Windows are sampled EVENLY across a chapter's whole span. Taking the head and
the tail elides the middle, and the middle is where the scene that the chapter
is actually about tends to be; that shortcut is how a clearly wrong name gets
produced.

The mechanical checks are boring and catch most of the bad output: length,
trailing punctuation, a chapter number echoed into the name, a timecode, the
wrong language, and -- importantly -- an empty result where the window has no
dialogue at all. An empty name is a correct answer there.

What does NOT ship is a namer. Naming well takes a person reasoning over
the windows, not a program, and shipping a namer that does not
exist would be the most misleading thing in this repository. The command
either calls a program the user configured, or it is absent.

Planned public API:
    build_windows(media, marks, *, budget_chars, sample='even') -> list[Window]
    check_names(names, windows, *, rules=NAMING_RULES) -> list[Grade]

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
