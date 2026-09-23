"""What state the chapter marks are in: none, generated, named, or mixed.

The classification is the useful part, and it is three rules deep.

**Generated names carry no information.** ``Chapter 5`` is what a server
writes when a file has marks and nothing to call them. A file full of them is
a file with marks and no names, and counting it as named is how a library
looks finished.

**Evenly spaced marks were not put there by anyone.** Some tools cut a file
into marks every five or ten minutes. They are real marks, they are at
plausible times, and they correspond to nothing in the film. The signature is
the spacing: the gaps between real chapter marks vary, and the gaps between
generated ones do not. The test here is the coefficient of variation of the
gaps -- their standard deviation over their mean -- which is scale-free, so
one threshold works for a ninety-minute film and a twenty-minute episode
alike.

**Mixed is its own answer.** A file where four marks of sixteen have names is
neither named nor unnamed, and rolling it into either makes the count a lie
in one direction or the other.

The threshold below was chosen on synthetic material and is a default, not a
constant of nature; the caveat says so and the argument is there to change.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any, Literal

from ..dto import TICKS_PER_SECOND, normalise_chapter_name
from ..report import Column, Survey

__all__ = ["FIXED_INTERVAL_CV", "State", "chapter_state", "classify", "gap_variation"]

State = Literal["none", "generated", "mixed", "named"]

#: Below this coefficient of variation the gaps are too regular to have been
#: chosen by anyone. Fitted on generated grids and on hand-made chapter sets;
#: a default, not a universal.
FIXED_INTERVAL_CV = 0.05

#: Fewer marks than this and the spacing test says nothing: two gaps can be
#: equal by coincidence.
MIN_MARKS_FOR_SPACING = 4


def gap_variation(ticks: Sequence[int]) -> float | None:
    """How irregular the spacing is, as a scale-free number.

    ``None`` when there are too few marks to say anything. Zero means
    perfectly even, which is the signature of a grid rather than of a film.
    """
    if len(ticks) < MIN_MARKS_FOR_SPACING:
        return None
    gaps = [b - a for a, b in pairwise(ticks) if b > a]
    if len(gaps) < 2:
        return None
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return None
    return statistics.stdev(gaps) / mean


def classify(chapters: Sequence[Mapping[str, Any]]) -> tuple[State, int, int]:
    """The state, the number of marks, and how many of them carry a real name."""
    if not chapters:
        return "none", 0, 0
    named = sum(
        1 for chapter in chapters
        if normalise_chapter_name(chapter.get("Name")) is not None
    )
    if named == 0:
        return "generated", len(chapters), 0
    if named == len(chapters):
        return "named", len(chapters), named
    return "mixed", len(chapters), named


def chapter_state(items: Sequence[Mapping[str, Any]]) -> Survey:
    """One row per item that can have marks, with the state of its marks."""
    columns = [
        Column("item", "Item"),
        Column("type", "Type"),
        Column("state", "State"),
        Column("marks", "Marks", kind="number"),
        Column("named", "With a name", kind="number"),
        Column("mean_gap_s", "Mean gap (s)", kind="number", places=1),
        Column("gap_variation", "Gap variation", kind="number", places=3),
        Column("fixed_interval", "Evenly spaced", kind="bool"),
    ]
    rows: list[dict[str, Any]] = []
    states: dict[str, int] = {"none": 0, "generated": 0, "mixed": 0, "named": 0}
    even = 0
    unmeasurable = 0

    for item in items:
        if item.get("Type") not in {"Movie", "Episode"}:
            continue
        chapters = list(item.get("Chapters") or [])
        state, marks, named = classify(chapters)
        ticks = [int(c.get("StartPositionTicks") or 0) for c in chapters]
        variation = gap_variation(ticks)
        gaps = [b - a for a, b in pairwise(ticks)]
        mean_gap = (
            statistics.fmean(gaps) / TICKS_PER_SECOND if gaps else None
        )
        fixed = variation is not None and variation < FIXED_INTERVAL_CV
        even += bool(fixed)
        unmeasurable += marks > 0 and variation is None
        states[state] += 1
        rows.append({
            "item": item.get("Name"),
            "type": item.get("Type"),
            "state": state,
            "marks": marks,
            "named": named,
            "mean_gap_s": mean_gap,
            "gap_variation": variation,
            "fixed_interval": fixed,
        })

    summary: dict[str, Any] = {"items with marks of any kind":
                               len(rows) - states["none"]}
    summary.update({f"{state}": count for state, count in states.items()})
    summary["evenly spaced, so generated by a tool"] = even
    summary["too few marks to judge the spacing"] = unmeasurable
    return Survey(
        name="Chapter state",
        about="Whether each item has marks, whether they are named, and whether "
              "a tool put them there.",
        columns=columns,
        rows=rows,
        summary=summary,
        scope={"types": "Movie, Episode", "items seen": len(items)},
        caveats=[
            "Read from the catalogue. An item whose file has marks the server has "
            "not read yet is reported as having none.",
            f"Evenly spaced means the gaps between marks vary by less than "
            f"{FIXED_INTERVAL_CV:.0%} of their mean. That threshold was chosen on "
            "synthetic material and is a default; a film genuinely cut into equal "
            "parts trips it, and a grid with one mark nudged by hand does not.",
            "A name that cannot be told apart from a generated one is counted as "
            "generated, which understates rather than overstates how named a "
            "collection is.",
        ],
    )
