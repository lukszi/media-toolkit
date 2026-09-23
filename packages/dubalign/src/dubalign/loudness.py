"""dubalign.loudness -- match what a listener hears, not what a meter peaks at.

When one stretch of a built track comes from a different transfer, the join is
heard as a level change long before it is heard as a sync error. Matching the
two is a single number -- a constant gain on the segment that came from
elsewhere -- and the only question is what that number is measured against.

**Integrated loudness, not peak.** Two masters of one programme routinely
differ by four or five decibels in loudness and by nothing at all in peak,
because one of them is limited and the other is not. Matching peaks leaves an
audible step exactly where the listener is already listening for one.

**Over the same content.** The gain is measured across a stretch that both
sources carry, not across each source's whole runtime: one of them may include
material the other does not, and averaging that in measures the difference
between two different programmes.

**A constant gain, and nothing else.** No compression, no limiting, no
normalisation. If the two sources have genuinely different dynamic ranges --
and they often do, by a few loudness units of range over identical content --
a constant gain is the honest fix and the difference
is worth writing down rather than processing away.

The measurement itself goes through the external program, which implements the
broadcast standard. Where it is absent, :func:`match_gain_db` falls back to a
plain level difference, which is a worse answer and says so: it is right for
two masters of the same mix and wrong wherever the spectra differ.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mkvkit.config import Config
from mkvkit.run import Runner, default_runner
from numpy.typing import NDArray

from .align import FULL_RATE

__all__ = [
    "Loudness",
    "integrated_loudness",
    "match_gain_db",
    "parse_loudness",
    "rms_gain_db",
]

log = logging.getLogger(__name__)

#: How far apart two stretches have to be before a gain is worth applying.
#: Below this the correction is inaudible and the edit is not worth making.
MIN_GAIN_DB = 1.0

_INTEGRATED = re.compile(r"I:\s*(-?[\d.]+)\s*LUFS")
_RANGE = re.compile(r"LRA:\s*(-?[\d.]+)\s*LU")
_PEAK = re.compile(r"Peak:\s*(-?[\d.a-z]+)\s*dBFS")


@dataclass(frozen=True)
class Loudness:
    """What a stretch of programme measures, by the broadcast standard."""

    integrated_lufs: float
    range_lu: float | None = None
    true_peak_dbfs: float | None = None

    def describe(self) -> str:
        parts = [f"{self.integrated_lufs:.2f} LUFS"]
        if self.range_lu is not None:
            parts.append(f"range {self.range_lu:.2f} LU")
        if self.true_peak_dbfs is not None:
            parts.append(f"true peak {self.true_peak_dbfs:+.2f} dBFS")
        return ", ".join(parts)


def parse_loudness(text: str) -> Loudness | None:
    """Read the summary the program prints. Separated so it is testable."""
    tail = text[text.rfind("Integrated loudness") :] if "Integrated loudness" in text else text
    integrated = _INTEGRATED.search(tail)
    if integrated is None:
        return None
    found_range = _RANGE.search(tail)
    peak = _PEAK.search(tail)
    peak_value: float | None = None
    if peak is not None:
        try:
            parsed = float(peak.group(1))
        except ValueError:
            parsed = float("nan")
        # A peak the meter could not express is not a peak. Carrying an
        # infinity forward would make every comparison downstream true.
        peak_value = parsed if math.isfinite(parsed) else None
    return Loudness(
        integrated_lufs=float(integrated.group(1)),
        range_lu=float(found_range.group(1)) if found_range else None,
        true_peak_dbfs=peak_value,
    )


def integrated_loudness(
    block: NDArray[np.floating],
    *,
    sample_rate: int = FULL_RATE,
    layout: str | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Loudness | None:
    """Measure one block of samples, by piping it through the meter.

    ``None`` when the program did not produce a reading, which is a reason to
    fall back rather than to stop: a gain that cannot be measured properly is
    still better estimated than not applied.

    The block goes through a scratch file rather than a pipe. A pipe would be
    tidier and is not worth a second way of running an external program: one
    runner, used the same way everywhere, is what makes every other call in
    this package testable without one.
    """
    run = runner if runner is not None else default_runner(config)
    data = np.ascontiguousarray(np.asarray(block, dtype=np.float32))
    channels = data.shape[1] if data.ndim > 1 else 1
    with tempfile.TemporaryDirectory(prefix="dubalign-loudness-") as scratch:
        raw = Path(scratch) / "block.f32le"
        data.tofile(raw)
        args = [
            "-nostdin", "-v", "info", "-f", "f32le", "-ar", str(sample_rate),
            "-ac", str(channels),
        ]
        if layout:
            args += ["-channel_layout", layout]
        args += [
            "-i", str(raw), "-filter_complex", "ebur128=peak=true", "-f", "null", "-",
        ]
        result = run("ffmpeg", args)
    return parse_loudness(result.stderr or "")


def rms_gain_db(
    reference: NDArray[np.floating], other: NDArray[np.floating]
) -> float:
    """How much to lift ``other`` so its level matches ``reference``.

    A level difference, not a loudness difference. Right for two masters of one
    mix, wrong wherever the two differ in spectrum -- which is why the
    measured-loudness path above exists and this is the fallback.
    """
    left = np.asarray(reference, dtype=np.float64)
    right = np.asarray(other, dtype=np.float64)
    a = float(np.sqrt(np.mean(left * left))) if left.size else 0.0
    b = float(np.sqrt(np.mean(right * right))) if right.size else 0.0
    if a <= 0.0 or b <= 0.0:
        return 0.0
    return 20.0 * math.log10(a / b)


def match_gain_db(
    reference: NDArray[np.floating],
    other: NDArray[np.floating],
    *,
    sample_rate: int = FULL_RATE,
    layout: str | None = None,
    runner: Runner | None = None,
    config: Config | None = None,
    min_gain_db: float = MIN_GAIN_DB,
) -> float:
    """The constant gain that puts ``other`` at ``reference``'s loudness.

    Zero when the two are already within ``min_gain_db``: an edit that nobody
    can hear is an edit that should not be in the plan, because every number in
    a plan is something a reader has to account for.
    """
    gain = 0.0
    try:
        left = integrated_loudness(
            reference, sample_rate=sample_rate, layout=layout, runner=runner, config=config
        )
        right = integrated_loudness(
            other, sample_rate=sample_rate, layout=layout, runner=runner, config=config
        )
    except Exception as exc:  # any failure to measure means fall back, not stop
        log.info("the loudness meter did not run (%s); using levels instead", exc)
        left = right = None
    if left is not None and right is not None:
        gain = left.integrated_lufs - right.integrated_lufs
        log.info(
            "loudness: reference %s, other %s -> %+0.2f dB",
            left.describe(), right.describe(), gain,
        )
    else:
        gain = rms_gain_db(reference, other)
        log.info("levels only: %+0.2f dB (this is the fallback, not a loudness match)", gain)
    return 0.0 if abs(gain) < min_gain_db else gain
