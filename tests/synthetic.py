"""A pair of signals with a drift and a step in it, and the answers written down.

Every alignment test in this suite needs two signals that differ in a way
somebody chose, so that what the measurement reports can be compared against
what was put there. Building that pair in memory rather than on disk means the
whole measurement and planning chain is testable with nothing installed -- no
program, no media, no fixtures.

The pair carries all three defects at once, because a measurement that can only
tell them apart when they arrive one at a time is not much use:

* a **constant offset** at the head, from silence the other source carries and
  the reference does not;
* a **step**, where a stretch of the reference is simply missing from the other
  source, so the offset jumps at one instant;
* a **rate difference** after the step, so the offset slides from there on, far
  enough to leave the tolerance by the end.

The content is not plain noise. Plain noise has no onsets, and a signal with no
onsets has no envelope worth correlating -- resample it and nothing matches. So
the reference is noise shaped by decaying bursts at irregular intervals, which
is the property of real programme material that these measurements live on.
Irregular on purpose: a regular pulse matches itself one pulse later.

``true_lag`` is the answer key. Nothing in the package sees it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np
from dubalign.align import ANALYSIS_RATE, Signal
from dubalign.pal import warp

#: The noise source is seeded, so the same pair comes out on every machine.
SEED = 20260923

#: The defects, in one place, because they are the answer key. The rate is the
#: one a whole-sample rate change gives at 48 kHz (48024/48000), so the same
#: pair can be built by a program as well as in memory.
DURATION_S = 120.0
HEAD_LAG_S = 0.120
STEP_AT_S = 20.0
STEP_DROP_S = 0.040
RATE_AFTER = 48_024 / 48_000

#: Roughly how often a burst starts, and how fast it decays.
BURST_INTERVAL_S = 0.30
BURST_DECAY_S = 0.12


def noise(seconds: float, sr: int = ANALYSIS_RATE, seed: int = SEED) -> Signal:
    """Flat noise. Enough for a waveform correlation, useless for an envelope."""
    rng = np.random.default_rng(seed)
    return np.asarray(rng.standard_normal(int(seconds * sr)) * 0.2, dtype=np.float64)


def programme(seconds: float, sr: int = ANALYSIS_RATE, seed: int = SEED) -> Signal:
    """Noise shaped by decaying bursts at irregular intervals.

    Stands in for programme material in the only respect these measurements
    care about: it starts and stops, at times that do not repeat.
    """
    rng = np.random.default_rng(seed)
    samples = int(seconds * sr)
    carrier = np.convolve(rng.standard_normal(samples), np.ones(4) / 4.0, mode="same")
    shape = np.zeros(samples)
    tau = int(BURST_DECAY_S * sr)
    decay = np.exp(-np.arange(tau * 4) / tau)
    starts = np.cumsum(
        rng.exponential(BURST_INTERVAL_S, size=int(seconds / BURST_INTERVAL_S) + 20)
    )
    for start in starts:
        index = int(start * sr)
        if index >= samples:
            break
        end = min(samples, index + len(decay))
        shape[index:end] += decay[: end - index] * rng.uniform(0.4, 1.0)
    return np.asarray(carrier * (0.01 + shape) * 0.2, dtype=np.float64)


@dataclass(frozen=True)
class SyntheticPair:
    """Two signals and the offset between them at every instant."""

    reference: Signal
    other: Signal
    sr: int
    head_lag_s: float
    step_at_s: float
    step_drop_s: float
    rate_after: float

    @property
    def slope(self) -> float:
        """Seconds of lag gained per second, after the step."""
        return 1.0 / self.rate_after - 1.0

    @property
    def lag_after_step(self) -> float:
        return self.head_lag_s - self.step_drop_s

    @property
    def step_size_s(self) -> float:
        """How far the offset jumps at the step. Negative: the other runs early."""
        return -self.step_drop_s

    def true_lag(self, t: float) -> float:
        """Where the reference's content at ``t`` sits in the other source."""
        if t < self.step_at_s:
            return self.head_lag_s
        after = t - self.step_at_s - self.step_drop_s
        return self.head_lag_s + self.step_at_s + after / self.rate_after - t

    def true_lags(self, times: Signal) -> Signal:
        """The answer key as a curve, for comparing a whole measured series."""
        return np.asarray([self.true_lag(float(x)) for x in times], dtype=np.float64)


@functools.lru_cache(maxsize=4)
def synthetic_pair(
    *,
    sr: int = ANALYSIS_RATE,
    duration_s: float = DURATION_S,
    head_lag_s: float = HEAD_LAG_S,
    step_at_s: float = STEP_AT_S,
    step_drop_s: float = STEP_DROP_S,
    rate_after: float = RATE_AFTER,
    seed: int = SEED,
) -> SyntheticPair:
    """Build the pair by reading the reference at the positions the answer implies.

    Built once per set of arguments and process, and handed out read-only: the
    warp is the expensive part, several modules ask for the same pair, and an
    array nobody can write to is one no test can change under another.
    """
    reference = programme(duration_s, sr, seed)
    out_length = int(
        (head_lag_s + step_at_s + (duration_s - step_at_s - step_drop_s) / rate_after) * sr
    )
    u = np.arange(out_length, dtype=np.float64) / sr
    before = (u - head_lag_s) * sr
    after = (step_at_s + step_drop_s + (u - head_lag_s - step_at_s) * rate_after) * sr
    # Before the head silence ends, positions are negative and read as silence.
    positions = np.where(u < head_lag_s + step_at_s, before, after)
    other = warp(reference, positions)
    reference.setflags(write=False)
    other.setflags(write=False)
    return SyntheticPair(
        reference=reference,
        other=other,
        sr=sr,
        head_lag_s=head_lag_s,
        step_at_s=step_at_s,
        step_drop_s=step_drop_s,
        rate_after=rate_after,
    )


def interleave(
    x: Signal, channels: int = 6, centre: int = 2, seed: int = SEED + 1
) -> Signal:
    """Put one signal in the centre channel and something else in the rest.

    Enough of a multichannel block to exercise the channel choice: the centre
    carries the content, the others carry what is not it, and the low-frequency
    channel carries almost nothing -- the shape that makes picking the wrong
    channel a measurable mistake rather than a stylistic one.
    """
    rng = np.random.default_rng(seed)
    block = rng.standard_normal((len(x), channels)) * 0.01
    block[:, min(centre, channels - 1)] = x
    if channels > 3:
        block[:, 3] *= 0.001
    return np.asarray(block, dtype=np.float32)
