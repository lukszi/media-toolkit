"""dubalign.align -- the primitives every measurement in this package uses.

Everything else here is built out of four things.

**An onset envelope.** The short-term energy of a signal, logged, differenced
and half-wave rectified, at one kilohertz. What survives is where sound
*starts* -- transients, consonants, cuts, doors closing. What does not survive
is timbre, level and, usefully, language. Two different dubs of the same
picture share their onsets almost exactly; their waveforms share nothing.

**A normalised cross-correlation.** :func:`ncc_full` slides a short template
over a long signal and reports Pearson's r at every position. Normalised, so a
loud passage cannot outscore a quiet one merely by being loud, and computed
through one transform rather than a loop, so sliding a ten-second window over a
two-hour timeline is a second of arithmetic rather than an afternoon.

**A confidence measure that is not the correlation.** The peak's height says
how good the best match is. It does not say whether anything else matched
nearly as well -- and a periodic signal matches itself at every period. The
ratio of the peak to the *next* peak outside a guard band is what separates
"this is the alignment" from "this is one of forty equally good alignments",
and it is the number the rest of this package gates on.

**Two resolutions.** The envelope resolves to about a millisecond and searches
a whole timeline cheaply; the raw waveform resolves to a sample but only
searches a narrow bracket. Every measurement here is coarse-then-fine for that
reason, and never fine alone.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "ANALYSIS_RATE",
    "ENVELOPE_HOP",
    "ENVELOPE_RATE",
    "FULL_RATE",
    "MIN_R",
    "MIN_RATIO",
    "Peak",
    "Refinement",
    "Signal",
    "best_with_ratio",
    "load_mono",
    "ncc_full",
    "onset_env",
    "refine",
]

log = logging.getLogger(__name__)

#: The rate a full-rate signal is decoded at. Everything sample-exact is here.
FULL_RATE = 48_000
#: The rate the single-channel analysis copy is decoded at.
ANALYSIS_RATE = 16_000
#: How many analysis samples one envelope frame covers.
ENVELOPE_HOP = 16
#: The rate the onset envelope comes out at: one frame per millisecond.
ENVELOPE_RATE = ANALYSIS_RATE // ENVELOPE_HOP

#: A one-dimensional real signal. Everything here works in float64: the inputs
#: arrive as float32 and the correlations are sums of products over hundreds of
#: thousands of samples, where float32 loses digits that matter.
Signal = NDArray[np.float64]

#: The peak has to be this much taller than the next one to be believed, and
#: this well correlated. Both are defaults fitted against the known-answer
#: pairs in :mod:`dubalign.controls`, and both are arguments wherever they are
#: used rather than constants buried in a function.
MIN_RATIO = 1.5
MIN_R = 0.30


def load_mono(path: Path | str, *, dtype: str = "float32") -> Signal:
    """Read a headerless single-channel float dump as float64.

    Raw float is what the decoder writes and what this package measures on: no
    container, no seeking, and no ambiguity about where sample zero is.
    """
    raw = np.fromfile(str(path), dtype=dtype)
    return np.asarray(raw, dtype=np.float64)


def onset_env(x: Signal, hop: int = ENVELOPE_HOP, *, centre: bool = False) -> Signal:
    """Log-RMS energy, differenced and half-wave rectified.

    The result carries one sample per ``hop`` input samples, minus one for the
    difference. ``centre`` subtracts the mean, which matters when the envelope
    is correlated directly rather than through :func:`ncc_full`, which removes
    the mean itself.
    """
    if hop < 1:
        raise ValueError("hop must be at least one sample")
    frames = len(x) // hop
    if frames < 2:
        return np.zeros(0, dtype=np.float64)
    blocks = x[: frames * hop].reshape(frames, hop)
    energy = np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-10)
    rising = np.diff(np.log(energy))
    rising[rising < 0.0] = 0.0
    if centre:
        rising = rising - rising.mean()
    return np.asarray(rising, dtype=np.float64)


def ncc_full(
    a: Signal,
    b: Signal,
    *,
    b_fft: NDArray[np.complex128] | None = None,
    n_fft: int | None = None,
) -> Signal:
    """Pearson's r between ``a`` and every window of ``b`` of the same length.

    Returns ``r[k] = corr(a, b[k:k + len(a)])`` for every ``k`` from zero to
    ``len(b) - len(a)``. The numerator is one transform product; the
    denominator comes from prefix sums of ``b`` and of ``b ** 2``, so the
    normalisation costs one pass over the signal rather than a window's worth
    of arithmetic at every position.

    ``b_fft`` and ``n_fft`` let a caller transform a long signal once and reuse
    it for every template, which is what turns a whole-timeline search from
    minutes into seconds.
    """
    m, n = len(a), len(b)
    if m == 0 or n < m:
        raise ValueError(f"a template of {m} samples does not fit a signal of {n}")
    centred = a - a.mean()
    norm_a = float(np.linalg.norm(centred))
    if n_fft is None:
        n_fft = 1 << (n + m - 1).bit_length()
    if b_fft is None:
        b_fft = np.fft.rfft(b, n_fft)
    product = np.fft.irfft(b_fft * np.conj(np.fft.rfft(centred, n_fft)), n_fft)
    numerator = product[: n - m + 1]
    sums = np.concatenate(([0.0], np.cumsum(b)))
    squares = np.concatenate(([0.0], np.cumsum(b * b)))
    window_sum = sums[m:] - sums[:-m]
    window_squares = squares[m:] - squares[:-m]
    variance = np.maximum(window_squares - window_sum * window_sum / m, 1e-12)
    denominator = np.sqrt(variance) * max(norm_a, 1e-12)
    return np.asarray(numerator / denominator, dtype=np.float64)


@dataclass(frozen=True)
class Peak:
    """Where the best match is, how good it is, and whether it is alone.

    ``ratio`` is the one to read. A correlation of 0.95 with a second peak at
    0.94 is not a measurement; a correlation of 0.55 with nothing else above
    0.2 usually is.
    """

    index: int
    r: float
    ratio: float
    second: float

    def confident(self, *, min_ratio: float = MIN_RATIO, min_r: float = MIN_R) -> bool:
        """A clear peak that is also clearly the only one."""
        return self.ratio >= min_ratio and self.r >= min_r


def best_with_ratio(r: Signal, guard: int) -> Peak:
    """The tallest position, and the tallest one at least ``guard`` away from it.

    ``guard`` exists because the correlation of any real signal is smooth: the
    samples either side of the peak are nearly as tall as the peak itself, and
    comparing against them would report every measurement as ambiguous. It
    should be wide enough to clear the main lobe and no wider.
    """
    if len(r) == 0:
        raise ValueError("there is no correlation to take a peak from")
    index = int(np.argmax(r))
    masked = r.copy()
    low, high = max(0, index - guard), min(len(masked), index + guard + 1)
    masked[low:high] = -2.0
    outside = float(masked.max())
    second = outside if outside > -2.0 else 0.0
    return Peak(
        index=index,
        r=float(r[index]),
        ratio=float(r[index] / max(second, 1e-6)),
        second=second,
    )


@dataclass(frozen=True)
class Refinement:
    """A lag correction in seconds, with the correlation that produced it."""

    delta_s: float
    r: float
    ratio: float


def refine(
    a: Signal,
    b: Signal,
    t_a: float,
    t_b: float,
    *,
    dur: float = 2.0,
    search: float = 0.30,
    sr: int = ANALYSIS_RATE,
) -> Refinement | None:
    """Correct an approximate lag by correlating the raw waveform.

    Takes ``dur`` seconds of ``a`` from ``t_a``, searches ``+/- search``
    seconds around ``t_b`` in ``b``, and reports how far the true position is
    from ``t_b``. ``None`` when either window falls outside its signal, which
    is an ordinary thing to happen at the ends of a timeline and not an error.
    """
    start_a = round(t_a * sr)
    length = int(dur * sr)
    if start_a < 0 or start_a + length > len(a):
        return None
    template = a[start_a : start_a + length]
    start_b = round((t_b - search) * sr)
    span = length + 2 * int(search * sr)
    if start_b < 0 or start_b + span > len(b):
        return None
    correlation = ncc_full(template, b[start_b : start_b + span])
    peak = best_with_ratio(correlation, int(0.02 * sr))
    return Refinement(delta_s=(peak.index / sr) - search, r=peak.r, ratio=peak.ratio)
