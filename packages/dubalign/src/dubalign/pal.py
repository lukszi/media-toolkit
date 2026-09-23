"""dubalign.pal -- rate conversion, pitch, and the warp that corrects drift.

Two transfers of one programme are often not at the same speed. A transfer
made for 25-frame broadcast runs faster than the 24-frame original by exactly
25000/23976 -- which, written with the frame rates rather than their rounded
decimals, is **25025/24000**, and after cancelling, **1001/960**. Everything
here uses that fraction and never a decimal approximation of it. The
decimals are close enough to look harmless and are not: over a two-hour
programme, writing 25/23.976 costs 7.5 ms and writing 1.0427 costs 60 ms,
against a tolerance of 40 ms that everything else also has to fit inside.

Three things follow from it.

**Speed and pitch move together.** A faster transfer is also a sharper one,
by a semitone and a bit (``12 * log2(1001/960)`` = 0.72 semitones). Correcting
the speed without correcting the pitch is the mistake that makes a converted
dub sound wrong even when it is in sync, and it is what a tempo filter does.
:func:`speed_filter` writes the chain that moves both, through the sample
rate: resample to a rate the ratio divides exactly, reinterpret the result at
the original rate, resample back. Never a tempo filter, and never an
intermediate rate that had to be rounded to be written down.

**A rate difference is a drift, not an offset.** Two sources at the same
nominal rate can still differ by a few parts per million, which reads as a lag
that slides. :func:`warp` corrects the two together in a single pass: it takes
the *position in the source* that each output sample should come from, however
that position was arrived at, so a constant offset, a rate ratio and a measured
piecewise drift are one resampling rather than three.

**One pass, band-limited.** Resampling twice does not just cost twice; it
costs an extra interpolation error. The warp reads the source at fractional
positions through a Kaiser-windowed sinc, which is what a resampler is,
applied once at the positions the measurement asked for.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .align import Signal

__all__ = [
    "KERNEL_BETA",
    "KERNEL_HALF_WIDTH",
    "PAL_DOWN",
    "PAL_RATIO",
    "PAL_SEMITONES",
    "PAL_UP",
    "Spectrum",
    "log_spectrum",
    "pitch_shift",
    "resample",
    "scipy_available",
    "speed_filter",
    "warp",
]

log = logging.getLogger(__name__)

#: The ratio between a 25-frame transfer and a 24-frame one, exactly, in
#: lowest terms. 25025/24000 == 1001/960; the decimal is 1.0427083333...
PAL_UP = 1001
PAL_DOWN = 960
PAL_RATIO = PAL_UP / PAL_DOWN
#: How far the pitch moves with it: about three quarters of a semitone.
PAL_SEMITONES = 12.0 * math.log2(PAL_RATIO)

#: The interpolation kernel: a sinc windowed by a Kaiser window. The half
#: width buys stop-band rejection, the beta buys the shape of the transition;
#: 32 and 8.6 put the first side lobe about 90 dB down, which is below the
#: noise floor of anything this package will be asked to resample.
KERNEL_HALF_WIDTH = 32
KERNEL_BETA = 8.6

#: How many output samples one gather covers. Only a memory choice.
_WARP_BLOCK = 1 << 16


def scipy_available() -> bool:
    """Whether the optional numerical extra is installed.

    Everything in this module has a path that does not need it. The extra buys
    a polyphase resampler that handles the ends of a signal better and runs
    faster; without it the same job is done with transforms, and the two agree
    to well below the level anything here measures at.
    """
    try:
        import scipy.signal  # noqa: F401
    except ImportError:
        return False
    return True


def speed_filter(sr: int = 48_000, *, up: int = PAL_UP, down: int = PAL_DOWN) -> str:
    """The filter chain that changes speed and pitch together, exactly.

    >>> speed_filter()
    'aresample=50050,asetrate=48000,aresample=48000'

    Read it as: resample to a rate that is ``up/down`` times the real one, then
    *declare* the result to be at the real rate -- which stretches it in time
    and drops its pitch by the same factor -- then resample back to the output
    rate. Both intermediate rates are whole numbers by construction, because
    ``sr * up`` and ``sr * down`` both are; a chain that has to round one of
    them is not this conversion.

    A tempo filter would change the speed and leave the pitch, which is the
    one thing that must not happen to a transfer that was sped up mechanically.
    """
    if sr * up % down:
        raise ValueError(
            f"{sr} Hz cannot carry the ratio {up}/{down} without rounding; "
            "pick a rate the ratio divides"
        )
    return f"aresample={sr * up // down},asetrate={sr},aresample={sr}"


# ------------------------------------------------------------------ resampling
def resample(x: Signal, up: int, down: int) -> Signal:
    """Rational-ratio resampling: ``len(out) ~ len(x) * up / down``.

    Uses the polyphase resampler from the optional extra when it is installed,
    and a transform-domain resampling when it is not. Both are band-limited and
    both are exact for a rational ratio; the transform version treats the signal
    as one period, so it is the ends of a short signal that differ, never the
    middle.
    """
    if up < 1 or down < 1:
        raise ValueError("the ratio has to be positive")
    if up == down or len(x) == 0:
        return np.asarray(x, dtype=np.float64)
    try:
        from scipy.signal import resample_poly
    except ImportError:
        return _resample_fft(x, up, down)
    return np.asarray(resample_poly(x, up, down), dtype=np.float64)


def _resample_fft(x: Signal, up: int, down: int) -> Signal:
    """Resample by padding or truncating the spectrum. No dependency."""
    n = len(x)
    m = round(n * up / down)
    spectrum = np.fft.rfft(x)
    keep = min(len(spectrum), m // 2 + 1)
    resized = np.zeros(m // 2 + 1, dtype=np.complex128)
    resized[:keep] = spectrum[:keep]
    return np.asarray(np.fft.irfft(resized, m) * (m / n), dtype=np.float64)


def _kernel_table(
    half_width: int, beta: float, steps: int
) -> tuple[NDArray[np.float64], int]:
    """A table of windowed-sinc kernels, one per fractional position."""
    taps = 2 * half_width
    offsets = np.arange(-half_width + 1, half_width + 1, dtype=np.float64)
    fractions = np.arange(steps, dtype=np.float64) / steps
    grid = offsets[None, :] - fractions[:, None]
    kernels = np.sinc(grid) * np.kaiser(taps, beta)[None, :]
    sums = kernels.sum(axis=1, keepdims=True)
    return np.asarray(kernels / sums, dtype=np.float64), taps


def warp(
    x: Signal,
    positions: Signal,
    *,
    half_width: int = KERNEL_HALF_WIDTH,
    beta: float = KERNEL_BETA,
    steps: int = 256,
) -> Signal:
    """Read ``x`` at fractional sample positions, band-limited, in one pass.

    ``positions[i]`` is where output sample ``i`` comes from in ``x``, in
    samples. That one argument carries every correction there is: add a
    constant for an offset, multiply by a ratio for a rate difference, add a
    measured curve for a drift -- and because it is one array, they are applied
    together rather than one resampling after another.

    Positions outside the signal read as silence rather than as an error: the
    ends of a warped timeline routinely ask for samples that were never there,
    and a programme that stops half a second early is a better answer than a
    traceback.
    """
    if len(x) == 0:
        return np.zeros(len(positions), dtype=np.float64)
    kernels, taps = _kernel_table(half_width, beta, steps)
    # Zeros of at least a kernel width on both sides, so a position that falls
    # off either end reads silence instead of raising or wrapping around.
    padded = np.concatenate(
        [
            np.zeros(taps, dtype=np.float64),
            np.asarray(x, dtype=np.float64),
            np.zeros(taps, dtype=np.float64),
        ]
    )
    taken = np.arange(taps)
    out = np.empty(len(positions), dtype=np.float64)
    # In blocks: one gather is `taps` neighbours per output sample, and a
    # feature-length warp done in one go would ask for several gigabytes.
    for start in range(0, len(positions), _WARP_BLOCK):
        block = positions[start : start + _WARP_BLOCK]
        base = np.floor(block).astype(np.int64)
        fraction = block - base
        which = np.minimum((fraction * steps).astype(np.int64), steps - 1)
        index = (base - (half_width - 1) + taps)[:, None] + taken[None, :]
        gathered = padded[np.clip(index, 0, len(padded) - 1)]
        out[start : start + _WARP_BLOCK] = (gathered * kernels[which]).sum(axis=1)
    return out


# ----------------------------------------------------------------------- pitch
@dataclass(frozen=True)
class Spectrum:
    """A power spectrum on a log-frequency axis, flattened.

    Log frequency, because a pitch change is a *shift* there and a *scaling*
    anywhere else, and a shift is what a correlation can find. Flattened,
    because what identifies a pitch is the spacing of the partials and not the
    slope of the overall spectrum, which differs between any two encoders.
    """

    values: Signal
    bins_per_semitone: int
    f_min: float


def log_spectrum(
    x: Signal,
    sr: int,
    *,
    f_min: float = 80.0,
    f_max: float = 6000.0,
    bins_per_semitone: int = 50,
    segment: int = 8192,
) -> Spectrum:
    """Average power over overlapping segments, on a log-frequency grid."""
    if len(x) < segment:
        raise ValueError(f"need at least {segment} samples for a spectrum")
    step = segment // 2
    window = np.hanning(segment)
    frames = [x[i : i + segment] * window for i in range(0, len(x) - segment + 1, step)]
    power = np.mean([np.abs(np.fft.rfft(f)) ** 2 for f in frames], axis=0)
    freqs = np.fft.rfftfreq(segment, 1.0 / sr)
    logged = np.log(power + 1e-14)
    semitones = np.arange(0.0, 12.0 * math.log2(f_max / f_min), 1.0 / bins_per_semitone)
    grid = f_min * 2.0 ** (semitones / 12.0)
    on_grid = np.interp(grid, freqs, logged)
    # Flatten: subtract a three-semitone moving average, which is the spectral
    # slope, and keep what is left, which is the partial structure.
    width = 3 * bins_per_semitone
    kernel = np.ones(width) / width
    smooth = np.convolve(np.pad(on_grid, width, mode="edge"), kernel, mode="same")
    flattened = (on_grid - smooth[width:-width])[width:-width]
    return Spectrum(
        values=np.asarray(flattened, dtype=np.float64),
        bins_per_semitone=bins_per_semitone,
        f_min=f_min,
    )


@dataclass(frozen=True)
class PitchShift:
    """How far apart in pitch two excerpts are, and how sure the answer is."""

    semitones: float
    r: float
    ratio: float

    @property
    def speed_ratio(self) -> float:
        """The speed ratio that would account for the shift."""
        return float(2.0 ** (self.semitones / 12.0))


def pitch_shift(
    a: Signal,
    b: Signal,
    sr: int,
    *,
    max_semitones: float = 2.0,
    bins_per_semitone: int = 50,
) -> PitchShift:
    """How many semitones ``b`` sits above ``a``.

    Positive means ``b`` is sharper, which for two transfers of one programme
    means ``b`` is the faster one. Used to answer a question the lag cannot:
    whether a speed difference was applied to the sound as well as to the
    picture, or whether somebody corrected the tempo and left the pitch alone.
    """
    spec_a = log_spectrum(a, sr, bins_per_semitone=bins_per_semitone)
    spec_b = log_spectrum(b, sr, bins_per_semitone=bins_per_semitone)
    left = spec_a.values - spec_a.values.mean()
    right = spec_b.values - spec_b.values.mean()
    reach = int(max_semitones * bins_per_semitone)
    scores = np.empty(2 * reach + 1, dtype=np.float64)
    for i, shift in enumerate(range(-reach, reach + 1)):
        if shift >= 0:
            x, y = left[: len(left) - shift], right[shift:]
        else:
            x, y = left[-shift:], right[: len(right) + shift]
        size = min(len(x), len(y))
        x, y = x[:size], y[:size]
        denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
        scores[i] = float(np.dot(x, y) / denominator) if denominator > 1e-12 else 0.0
    best = int(np.argmax(scores))
    guard = bins_per_semitone // 4
    masked = scores.copy()
    masked[max(0, best - guard) : min(len(masked), best + guard + 1)] = -1.0
    second = float(masked.max())
    return PitchShift(
        semitones=(best - reach) / bins_per_semitone,
        r=float(scores[best]),
        ratio=float(scores[best] / max(second, 1e-6)),
    )
