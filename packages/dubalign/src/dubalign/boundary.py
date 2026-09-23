"""dubalign.boundary -- locate a cut by running two correlations at once.

Once the measurement says the offset is one value before some instant and
another value after it, finding that instant is not another search. It is a
comparison: read the two sources at the *old* offset and at the *new* one,
along a short stretch, and ask which of the two is right at each moment. The
old offset works up to the cut and stops working after it; the new one does
the reverse. Where the answer changes is the cut.

That is far cheaper than searching for the offset again at every position, and
far more stable: each correlation is a short local window at a *known* lag, so
there is no peak to pick and nothing to be ambiguous about.

**Where the crossing is taken matters more than it looks.** The obvious way --
the first moment the new offset scores above some number and the old one below
another -- depends on two thresholds, and on real material it fires early in a
quiet passage and late under music. The estimator here uses no threshold: it
picks the split that maximises the total evidence, ``sum(old before) +
sum(new after)``, over every possible split point. That is the same question
asked once rather than a few hundred times, and it cannot be tuned into a
different answer.

A cut placed even a third of a second from where it really is leaves the whole
step uncorrected for a third of a second. That is not a rounding error; it is
the difference between a join nobody notices and one that is heard as a word
arriving early.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .align import ANALYSIS_RATE, Signal

__all__ = [
    "Crossing",
    "Lag",
    "crossing",
    "running_r",
]

#: A lag either holds still or follows the drift. Both are allowed here,
#: because a boundary between two stretches that each slide has to be located
#: against lags that slide with them: a fixed number is only right in the
#: middle of its stretch, and half a second either side of that it correlates
#: at nothing.
Lag = float | Callable[[float], float]

log = logging.getLogger(__name__)

#: How long each local correlation window is, and how far apart they are
#: taken. Two hundred milliseconds is long enough to be a measurement and
#: short enough to resolve a cut to a few tens of milliseconds.
WINDOW_S = 0.200
STEP_S = 0.005


def running_r(
    reference: Signal,
    other: Signal,
    lag_s: Lag,
    t0: float,
    t1: float,
    *,
    sr: int = ANALYSIS_RATE,
    window_s: float = WINDOW_S,
    step_s: float = STEP_S,
) -> tuple[Signal, Signal]:
    """Local correlation at one fixed lag, along a stretch.

    Returns the times and the correlations. There is no search here: the lag is
    given, and the only question asked is how well it holds at each moment.
    """
    width = int(window_s * sr)
    if width < 2:
        raise ValueError("the window is shorter than two samples")
    times: list[float] = []
    values: list[float] = []
    t = t0
    while t + window_s <= t1:
        here = lag_s(t) if callable(lag_s) else lag_s
        index_a = round(t * sr)
        index_b = round((t + here) * sr)
        times.append(t)
        if (
            index_a < 0
            or index_b < 0
            or index_a + width > len(reference)
            or index_b + width > len(other)
        ):
            values.append(float("nan"))
            t += step_s
            continue
        left = reference[index_a : index_a + width]
        right = other[index_b : index_b + width]
        left = left - left.mean()
        right = right - right.mean()
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        values.append(
            float(np.dot(left, right) / denominator) if denominator > 1e-12 else float("nan")
        )
        t += step_s
    return (
        np.asarray(times, dtype=np.float64),
        np.asarray(values, dtype=np.float64),
    )


@dataclass(frozen=True)
class Crossing:
    """Where the old offset stops working and the new one starts.

    ``margin`` is how much better the split is than no split at all, in units
    of correlation summed over the stretch. A margin near zero means the two
    offsets are indistinguishable here, which happens in silence and under
    sustained music, and is a reason to widen the bracket rather than to
    believe the number.
    """

    t: float
    margin: float
    r_before: float
    r_after: float
    searched: tuple[float, float]

    @property
    def confident(self) -> bool:
        return self.margin > 0.0 and self.r_before > 0.2 and self.r_after > 0.2


def crossing(
    reference: Signal,
    other: Signal,
    lag_before: Lag,
    lag_after: Lag,
    t0: float,
    t1: float,
    *,
    sr: int = ANALYSIS_RATE,
    window_s: float = WINDOW_S,
    step_s: float = STEP_S,
) -> Crossing | None:
    """The instant at which the offset changes from one value to the other.

    Both correlations are computed along the whole bracket, and the split that
    maximises ``sum(before the split at the old lag) + sum(after it at the new
    lag)`` is the answer. No threshold, and nothing to tune.
    """
    times, before = running_r(
        reference, other, lag_before, t0, t1, sr=sr, window_s=window_s, step_s=step_s
    )
    _, after = running_r(
        reference, other, lag_after, t0, t1, sr=sr, window_s=window_s, step_s=step_s
    )
    usable = ~(np.isnan(before) | np.isnan(after))
    if usable.sum() < 4:
        log.debug("crossing: nothing measurable between %.3f and %.3f", t0, t1)
        return None
    times, before, after = times[usable], before[usable], after[usable]

    # Total evidence for a split at each position: the old lag explains
    # everything to its left, the new lag everything to its right.
    left_total = np.concatenate(([0.0], np.cumsum(before)))
    right_total = np.concatenate((np.cumsum(after[::-1])[::-1], [0.0]))
    score = left_total + right_total
    index = int(np.argmax(score))
    # No split at all: the old lag all the way, or the new lag all the way.
    baseline = max(float(left_total[-1]), float(right_total[0]))
    # The cut is between the last window that worked at the old lag and the
    # first that works at the new one; those windows overlap, so the boundary
    # is taken at the split itself and the step is what brackets it.
    t = float(times[min(index, len(times) - 1)])
    return Crossing(
        t=t,
        margin=float(score[index]) - baseline,
        r_before=float(np.mean(before[:index])) if index else 0.0,
        r_after=float(np.mean(after[index:])) if index < len(after) else 0.0,
        searched=(float(times[0]), float(times[-1])),
    )
