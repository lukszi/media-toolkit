"""dubalign.lag48k -- resolve the offset to whole samples at the full rate.

The envelope resolves a millisecond, the analysis waveform resolves about
sixty microseconds, and a splice needs neither: it needs an integer. Copying a
segment at an offset of "0.0416 seconds" means rounding somewhere, and rounding
in the wrong place is a sample of noise at a join that is otherwise perfect.

So the last measurement before a plan is taken on the full-rate signal, in a
bracket a few tens of milliseconds wide, and it comes back as a count of
samples. ``source_index = reference_index + offset``.

Two things about which channel it is taken on.

**Not the low-frequency channel, and not a difference of two channels.** A
dub's low-frequency and surround content is not the original programme's -- it
was mixed separately -- so correlating there measures two things that were
never meant to match and reports a confident number for it.

**The front centre, where there is one.** Dialogue lives there, dialogue is
where the onsets are, and it is the channel least likely to be a remix. Where
the chosen channel is silent in a particular window, the search falls back
rather than reporting an alignment of silence against silence.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .align import FULL_RATE, best_with_ratio, ncc_full

__all__ = [
    "PREFERRED_CHANNELS",
    "SampleLag",
    "SampleLagResult",
    "sample_exact_lag",
]

log = logging.getLogger(__name__)

#: Which channel of a six-channel layout to prefer, in order: front centre
#: first, then the two fronts. Never the low-frequency channel and never a
#: surround, for the reason in the module docstring.
PREFERRED_CHANNELS = (2, 0, 1)


@dataclass(frozen=True)
class SampleLag:
    """One sample-exact measurement, and where it was taken."""

    t: float
    offset_samples: int
    channel: int
    r: float
    ratio: float
    sample_rate: int

    @property
    def offset_s(self) -> float:
        return self.offset_samples / self.sample_rate


@dataclass(frozen=True)
class SampleLagResult:
    """Every measurement over a stretch, and what they agree on."""

    lags: tuple[SampleLag, ...]
    sample_rate: int

    @property
    def offsets(self) -> tuple[int, ...]:
        return tuple(x.offset_samples for x in self.lags)

    def median(self) -> int:
        if not self.lags:
            raise ValueError("nothing was measured")
        return int(statistics.median(self.offsets))

    def agrees(self) -> bool:
        """Every point read the same integer.

        This is the strongest thing a measurement in this package can say. Where
        it holds, the stretch is one constant offset and the splice is a plain
        copy; where it does not, there is either drift or a step inside the
        stretch, and the plan needs to know which before it places anything.
        """
        return len(set(self.offsets)) == 1

    def describe(self) -> str:
        if not self.lags:
            return "nothing was measured"
        distinct = sorted(set(self.offsets))
        median = self.median()
        return (
            f"{len(self.lags)} points, "
            f"{'one offset' if len(distinct) == 1 else f'{len(distinct)} distinct offsets'}: "
            f"median {median:+d} samples = {median / self.sample_rate:+.6f} s"
        )


def _pick_channel(block: NDArray[np.float32], preferred: Sequence[int]) -> int | None:
    width = int(block.shape[1]) if block.ndim > 1 else 1
    for channel in (*preferred, *range(width)):
        if channel < width and float(np.abs(block[:, channel]).max()) > 1e-4:
            return int(channel)
    return None


def sample_exact_lag(
    reference: NDArray[np.float32],
    other: NDArray[np.float32],
    base_s: float,
    times: Sequence[float],
    *,
    sample_rate: int = FULL_RATE,
    window_s: float = 3.0,
    search_s: float = 0.05,
    preferred: Sequence[int] = PREFERRED_CHANNELS,
) -> SampleLagResult:
    """Measure the offset in whole samples at several points in one stretch.

    Both arrays are interleaved full-rate signals shaped ``(frames, channels)``.
    ``base_s`` is where the coarse measurement said the offset is; the search
    only has to cover the error in that, which is why it can afford to run on
    the waveform.

    Several points rather than one, on purpose: one point cannot tell a
    constant offset from a drift, and the whole reason for this stage is to
    find out which of the two is being looked at.
    """
    length = int(window_s * sample_rate)
    reach = int(search_s * sample_rate)
    found: list[SampleLag] = []
    for t in times:
        index_a = round(t * sample_rate)
        if index_a < 0 or index_a + length > len(reference):
            continue
        channel = _pick_channel(reference[index_a : index_a + length], preferred)
        if channel is None:
            log.debug("t=%.1f: the reference is silent in every channel here", t)
            continue
        index_b = round((t + base_s) * sample_rate) - reach
        span = length + 2 * reach
        if index_b < 0 or index_b + span > len(other):
            continue
        window = np.asarray(reference[index_a : index_a + length, channel], dtype=np.float64)
        against = np.asarray(other[index_b : index_b + span, channel], dtype=np.float64)
        peak = best_with_ratio(ncc_full(window, against), max(1, int(0.002 * sample_rate)))
        found.append(
            SampleLag(
                t=float(t),
                offset_samples=index_b + peak.index - index_a,
                channel=channel,
                r=peak.r,
                ratio=peak.ratio,
                sample_rate=sample_rate,
            )
        )
    result = SampleLagResult(lags=tuple(found), sample_rate=sample_rate)
    log.info("sample-exact lag: %s", result.describe())
    return result
