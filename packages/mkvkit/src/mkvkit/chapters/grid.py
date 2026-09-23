"""mkvkit.chapters.grid -- does this chapter list describe the cut you have?

A published chapter list is only usable if its marks land where your file's
marks land. Comparing them is a two-line calculation and a great deal of
care, because the two lists can be right about different things:

**A transfer may be rate-converted.** The same feature exists at twenty-five
frames a second and at twenty-four-thousand-over-one-thousand-and-one, and
one is the other times :data:`RATE_RATIO` exactly. A list that looks wrong by
four per cent is not wrong, it is the other transfer -- so every comparison is
tried at the ratio, at its inverse, and at one.

**A constant offset is not a disagreement.** One list may start at the first
frame and the other after a distributor logo. The median difference is
therefore removed before the deviations are scored: what is being tested is
whether the *shape* of the two grids agrees, not where they start.

**Agreement of the marks says nothing about the names.** This module answers
one question -- do the marks fit? -- and the answer has a hard limit written
into it. A rate-converted candidate may be used **only** to copy names onto
marks the file already has (:func:`copy_names`), never to write marks into a
file that has none (:func:`adopt`, which refuses any scale but one). The
reason is the grid agreement itself: it is only evidence because there were
two grids to compare. With no marks in the file there is nothing to check the
candidate against, and a rate-converted list from a different cut fits nothing.

The name half of the question -- were these names typed against these marks?
-- is not answered here and is not answerable by arithmetic. A grid can match
perfectly while the names describe a scene a mark or more away.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from .xml import ChapterError, ChapterSet, is_generic_name

__all__ = [
    "COUNT_MISMATCH",
    "DIFFERS",
    "LOOSE",
    "LOOSE_TOLERANCE_S",
    "MATCH",
    "MATCH_TOLERANCE_S",
    "MIN_MARKS",
    "RATE_RATIO",
    "SCALES",
    "TOO_FEW",
    "GridMatch",
    "adopt",
    "copy_names",
    "grid_match",
    "runtime_match",
]

#: Twenty-five frames a second against twenty-four-thousand over one thousand
#: and one, exactly: 25 x 1001 / 24000. The rounded spellings of this ratio
#: that circulate are wrong by about a second over a feature, which is inside
#: the tolerance below and therefore invisible until it is not.
RATE_RATIO: Final = 25025 / 24000

#: Scale, and what to call it in a report.
SCALES: Final[tuple[tuple[float, str], ...]] = (
    (1.0, "as written"),
    (1.0 / RATE_RATIO, "rate-converted"),
    (RATE_RATIO, "rate-converted the other way"),
)

#: Worst deviation from the median offset that still counts as the same grid.
MATCH_TOLERANCE_S: Final = 2.0
#: Beyond the tolerance but close enough to be worth a person's attention.
LOOSE_TOLERANCE_S: Final = 5.0
#: Fewer marks than this and the comparison proves nothing.
MIN_MARKS: Final = 3

MATCH: Final = "match"
LOOSE: Final = "loose"
DIFFERS: Final = "differs"
COUNT_MISMATCH: Final = "count-mismatch"
TOO_FEW: Final = "too-few-marks"


@dataclass(frozen=True)
class GridMatch:
    """How well a candidate's marks fit the file's, at the best scale tried."""

    verdict: str
    scale: float = 1.0
    scale_name: str = "as written"
    offset_s: float = 0.0
    max_deviation_s: float | None = None
    p90_deviation_s: float | None = None
    marks: int = 0
    candidate_marks: int = 0

    @property
    def usable_for_names(self) -> bool:
        """Only a match may have its names copied onto the file's own marks."""
        return self.verdict == MATCH

    @property
    def rate_converted(self) -> bool:
        return self.scale != 1.0

    def __str__(self) -> str:
        if self.max_deviation_s is None:
            return f"{self.verdict} ({self.marks} vs {self.candidate_marks} marks)"
        return (
            f"{self.verdict} at {self.scale_name}: worst deviation "
            f"{self.max_deviation_s:.3f} s, offset {self.offset_s:+.3f} s, "
            f"{self.marks} marks"
        )


def _starts(value: ChapterSet | Sequence[float]) -> tuple[float, ...]:
    if isinstance(value, ChapterSet):
        return value.starts_s
    return tuple(float(v) for v in value)


def grid_match(
    marks: ChapterSet | Sequence[float],
    candidate: ChapterSet | Sequence[float],
    *,
    tolerance_s: float = MATCH_TOLERANCE_S,
    loose_s: float = LOOSE_TOLERANCE_S,
    min_marks: int = MIN_MARKS,
    scales: Sequence[tuple[float, str]] = SCALES,
) -> GridMatch:
    """Compare two grids at every scale and report the best fit.

    Equal counts and at least ``min_marks`` marks, or there is nothing to
    compare: two lists of two marks agree by accident.
    """
    mine = _starts(marks)
    theirs = _starts(candidate)
    if len(mine) != len(theirs):
        return GridMatch(COUNT_MISMATCH, marks=len(mine), candidate_marks=len(theirs))
    if len(mine) < min_marks:
        return GridMatch(TOO_FEW, marks=len(mine), candidate_marks=len(theirs))

    best: GridMatch | None = None
    for scale, name in scales:
        scaled = [t * scale for t in theirs]
        differences = [a - b for a, b in zip(mine, scaled, strict=True)]
        offset = statistics.median(differences)
        deviations = sorted(abs(d - offset) for d in differences)
        worst = deviations[-1]
        p90 = deviations[int(0.9 * (len(deviations) - 1))]
        verdict = (
            MATCH if worst <= tolerance_s else LOOSE if worst <= loose_s else DIFFERS
        )
        candidate_match = GridMatch(
            verdict=verdict,
            scale=scale,
            scale_name=name,
            offset_s=offset,
            max_deviation_s=worst,
            p90_deviation_s=p90,
            marks=len(mine),
            candidate_marks=len(theirs),
        )
        # A deviation of zero is the best possible answer, so the comparison
        # is written out rather than leaning on truthiness -- which would read
        # an exact match as "nothing found yet" and prefer a worse scale.
        if best is None or best.max_deviation_s is None:
            best = candidate_match
        elif worst < best.max_deviation_s:
            best = candidate_match
    if best is None:
        raise ChapterError("no scale was offered to compare at")
    return best


def runtime_match(
    runtime_s: float,
    candidate_runtime_s: float,
    *,
    tolerance_s: float = 30.0,
    scales: Sequence[tuple[float, str]] = SCALES,
) -> tuple[bool, str]:
    """Whether two runtimes are the same feature, at any of the scales.

    A first filter, not evidence: two unrelated features routinely run to
    within half a minute of each other, which is why a runtime match alone
    has never been allowed to select a candidate here.
    """
    for scale, name in scales:
        if abs(runtime_s - candidate_runtime_s * scale) <= tolerance_s:
            return True, name
    return False, "no scale fits"


def copy_names(
    marks: ChapterSet, candidate: ChapterSet, match: GridMatch
) -> ChapterSet:
    """The file's own marks, carrying the candidate's names.

    The times are the file's, always. That is what makes this the safe
    operation even for a rate-converted candidate: the worst case is a wrong
    name on a mark that is exactly where it was.
    """
    if not match.usable_for_names:
        raise ChapterError(
            f"the grids do not agree ({match}); copying names onto marks is only "
            "defensible where the grid agreement proves the candidate describes "
            "this cut"
        )
    names = [name if name and not is_generic_name(name) else None
             for name in candidate.names]
    return replace(marks.renamed(names), source=candidate.source)


def adopt(candidate: ChapterSet, match: GridMatch | None = None) -> ChapterSet:
    """A candidate's marks *and* names, for a file that has no marks of its own.

    Refused for anything rate-converted. With no marks in the file there is no
    grid to check the candidate against, so the only thing standing between a
    rate-converted list and the wrong cut is a runtime that agrees to within
    half a minute -- which is not evidence.
    """
    if match is not None and match.rate_converted:
        raise ChapterError(
            "a rate-converted candidate may only have its names copied onto marks "
            "the file already has; writing its marks into a file with none is a "
            "guess about which transfer it describes"
        )
    return candidate
