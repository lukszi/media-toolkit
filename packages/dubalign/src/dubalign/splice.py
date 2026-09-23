"""dubalign.splice -- build the output the plan describes.

Three things happen here and nothing else. Each segment is read from its source
at the position the plan gives, which carries the offset and the rate
difference together, so a stretch that is both shifted and sliding is one pass
over the material. Each segment's gain is applied. And at every seam the two
sides are crossfaded.

**The crossfade is equal power, and short.** Sine and cosine rather than a
straight line, because two signals that are not identical add in power: a
linear fade through the middle of a join leaves a dip of three decibels exactly
where the listener is already listening for one. Twenty milliseconds rather
than two hundred, because a long fade is audible as a swell even when the
alignment either side is perfect -- the ear notices the modulation, not the
edit.

**A segment whose rate is exactly one is copied, not resampled.** Reading
samples through an interpolation kernel when the positions are whole numbers
would add a filter to material that did not need one. The check is explicit
rather than incidental: most of a spliced programme usually is a plain copy,
and it should be provably so.

**Nothing is normalised, compressed or limited.** A gain is a constant number
of decibels on a named segment, decided by the loudness measurement and written
into the plan where it can be read. Anything cleverer would make the output
impossible to check against its sources.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Mapping

import numpy as np
from numpy.typing import NDArray

from .pal import warp
from .plan import PlanError, SplicePlan

__all__ = [
    "SpliceError",
    "as_block",
    "equal_power",
    "splice",
]

log = logging.getLogger(__name__)


class SpliceError(PlanError):
    """The plan cannot be built from the sources it was given."""


def as_block(x: NDArray[np.floating], channels: int) -> NDArray[np.float64]:
    """Any signal as ``(frames, channels)`` float64."""
    block = np.asarray(x, dtype=np.float64)
    if block.ndim == 1:
        block = block[:, None]
    if block.shape[1] != channels:
        raise SpliceError(
            f"a source with {block.shape[1]} channel(s) cannot build "
            f"{channels}-channel output"
        )
    return block


def equal_power(n: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """The two halves of an equal-power crossfade, ``n`` samples long.

    Their squares sum to one at every position, which is what keeps the level
    steady through the join when the two sides are not the same signal.
    """
    if n < 1:
        raise SpliceError("a crossfade needs at least one sample")
    x = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.cos(x * np.pi / 2.0), np.sin(x * np.pi / 2.0)


def _read(
    block: NDArray[np.float64], start: int, end: int, offset: int, ratio: float,
    anchor: int,
) -> NDArray[np.float64]:
    """One segment's worth of source, shifted and rate-corrected in one pass."""
    length = end - start
    if length <= 0:
        return np.zeros((0, block.shape[1]), dtype=np.float64)
    if ratio == 1.0:
        # A plain copy. Padding rather than clipping, so a segment that reaches
        # past the end of its source produces silence there instead of a
        # shorter output that everything downstream would then measure wrongly.
        first = start + offset
        out = np.zeros((length, block.shape[1]), dtype=np.float64)
        take_from = max(0, first)
        take_to = min(len(block), first + length)
        if take_to > take_from:
            out[take_from - first : take_to - first] = block[take_from:take_to]
        return out
    frames = np.arange(start, end, dtype=np.float64)
    positions = (anchor + offset) + (frames - anchor) * ratio
    return np.stack(
        [warp(block[:, c], positions) for c in range(block.shape[1])], axis=1
    )


def splice(
    plan: SplicePlan, sources: Mapping[str, NDArray[np.floating]]
) -> NDArray[np.float64]:
    """Build the whole output, segments then seams.

    The body is written first as a hard copy of each segment, and the
    crossfades are patched over the seams afterwards. Doing it in that order
    means a seam is a local edit to material that is already correct, which is
    also how a plan is corrected by hand: change one seam, rebuild, and nothing
    else in the output moves.
    """
    plan.check()
    missing = sorted({s.source for s in plan.segments} - set(sources))
    if missing:
        raise SpliceError(f"the plan needs sources that were not given: {', '.join(missing)}")
    blocks = {name: as_block(value, plan.channels) for name, value in sources.items()}
    out = np.zeros((plan.total_frames, plan.channels), dtype=np.float64)

    for segment, (start, end) in zip(plan.segments, plan.bounds(), strict=True):
        gain = 10.0 ** (segment.gain_db / 20.0)
        piece = _read(
            blocks[segment.source], start, end, segment.offset_samples,
            segment.rate_ratio, segment.anchor_frame,
        )
        out[start:end] = piece * gain if segment.gain_db else piece
        log.info(
            "%s: %.3f - %.3f s from %s%s",
            segment.label, start / plan.sample_rate, end / plan.sample_rate,
            segment.source, " (rate corrected)" if segment.drifts else "",
        )

    for index, seam in enumerate(plan.seams):
        left, right = plan.segments[index], plan.segments[index + 1]
        centre = round(seam.t * plan.sample_rate)
        half = max(1, round(seam.half_width_s * plan.sample_rate))
        a, b = max(0, centre - half), min(plan.total_frames, centre + half)
        if b - a < 2:
            continue
        before = _read(
            blocks[left.source], a, b, left.offset_samples, left.rate_ratio,
            left.anchor_frame,
        ) * 10.0 ** (left.gain_db / 20.0)
        after = _read(
            blocks[right.source], a, b, right.offset_samples, right.rate_ratio,
            right.anchor_frame,
        ) * 10.0 ** (right.gain_db / 20.0)
        fade_out, fade_in = equal_power(b - a)
        out[a:b] = before * fade_out[:, None] + after * fade_in[:, None]
        log.info(
            "seam %d at %.4f s: %.0f ms crossfade, %s -> %s",
            index + 1, seam.t, seam.half_width_s * 2000.0, left.source, right.source,
        )

    peak = float(np.abs(out).max()) if plan.total_frames else 0.0
    log.info(
        "built %d frames = %.3f s, peak %.4f (%.2f dBFS)",
        plan.total_frames, plan.duration_s, peak,
        20.0 * np.log10(peak) if peak > 0 else float("-inf"),
    )
    return out
