"""dubalign.controls -- the known-answer harness, run before anything is trusted.

A measurement pipeline that has never been given a question whose answer is
already known is not evidence. It is a number generator that has not yet been
caught. Everything in this package is checked against answers written down
first, and this module is where that check lives.

Four controls, all synthetic, all fast.

**Zero.** A track measured against itself must read 0.000. This catches a
whole family of off-by-one errors in the indexing, and it is the only control
that does not need anything to be built.

**A known offset.** A pair built with a delay somebody chose must give that
delay back, to the sample. Getting the sign wrong is the single most common
mistake in this kind of code and it is invisible on symmetric material.

**The same answer twice.** The same pair, measured at two different starting
points, must give the same answer. This is the one that catches a decoder
landing somewhere other than where it was asked to land -- an error that is
perfectly consistent within a single measurement and different between two.

**A shift of the data, not of the window.** The last one is the subtle one,
and it is the reason the others are not enough. If the reference window starts
``w`` earlier but the search only reaches ``+/-w``, then every measurable lag
lies between ``-w`` and about zero: positive errors read as nothing, and a
scan of a badly broken pair comes back looking immaculate. The symptom is a
run of points all reading the same small positive number. The only way to
catch it is to move the **data** by a known amount and confirm the measurement
follows -- moving the search window instead cancels out and proves nothing.

The array controls need nothing installed. The file controls run the same
questions through the real programs, because the arithmetic being right does
not prove that decoding a container gives the arithmetic what it thinks.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mkvkit.config import Config
from mkvkit.run import Runner, default_runner

from .align import ANALYSIS_RATE, Signal, best_with_ratio, ncc_full
from .densemap import dense_map
from .epk_align import correct_lag
from .probe import first_packet_time

__all__ = [
    "Control",
    "ControlReport",
    "array_controls",
    "file_controls",
    "measure_offset_ms",
]

log = logging.getLogger(__name__)

#: The delay the built pair carries, in milliseconds. A round number so a
#: wrong answer is obvious at a glance in a log.
KNOWN_OFFSET_MS = 250.0
#: How far a control may be from its answer before it has failed. One
#: millisecond: the controls are synthetic and there is nothing to excuse.
TOLERANCE_MS = 1.0
#: How much the second measurement of the same pair is moved along by.
RESTART_S = 3.0


@dataclass(frozen=True)
class Control:
    """One question with a known answer, and what came back."""

    name: str
    expected_ms: float
    measured_ms: float | None
    tolerance_ms: float = TOLERANCE_MS
    note: str = ""

    @property
    def passed(self) -> bool:
        if self.measured_ms is None:
            return False
        return abs(self.measured_ms - self.expected_ms) <= self.tolerance_ms

    def describe(self) -> str:
        got = "nothing" if self.measured_ms is None else f"{self.measured_ms:+.3f} ms"
        mark = "PASS" if self.passed else "FAIL"
        note = f"  ({self.note})" if self.note else ""
        return (
            f"{mark}  {self.name:<44} expected {self.expected_ms:+.3f} ms, "
            f"got {got}{note}"
        )


@dataclass(frozen=True)
class ControlReport:
    """Every control, and whether the pipeline may be believed."""

    controls: tuple[Control, ...]

    @property
    def passed(self) -> bool:
        return bool(self.controls) and all(c.passed for c in self.controls)

    @property
    def failures(self) -> tuple[Control, ...]:
        return tuple(c for c in self.controls if not c.passed)

    def describe(self) -> list[str]:
        lines = [c.describe() for c in self.controls]
        lines.append(
            f"{len(self.controls) - len(self.failures)} of {len(self.controls)} controls passed"
        )
        return lines


def measure_offset_ms(
    reference: Signal,
    other: Signal,
    t: float,
    *,
    sr: int = ANALYSIS_RATE,
    window_s: float = 4.0,
    search_s: float = 1.0,
) -> float | None:
    """The offset at one instant, measured the way everything else measures it.

    Deliberately the same primitives the rest of the package uses. A control
    that runs through a simpler path than the real code proves something about
    the simpler path.
    """
    index = round(t * sr)
    length = int(window_s * sr)
    reach = int(search_s * sr)
    if index < 0 or index + length > len(reference):
        return None
    start = index - reach
    span = length + 2 * reach
    if start < 0 or start + span > len(other):
        return None
    peak = best_with_ratio(
        ncc_full(reference[index : index + length], other[start : start + span]),
        max(1, int(0.002 * sr)),
    )
    return (start + peak.index - index) / sr * 1000.0


def array_controls(
    *,
    sr: int = ANALYSIS_RATE,
    offset_ms: float = KNOWN_OFFSET_MS,
    tolerance_ms: float = TOLERANCE_MS,
    duration_s: float = 30.0,
    seed: int = 20260923,
) -> ControlReport:
    """The four controls, on signals built here. Nothing installed is needed."""
    rng = np.random.default_rng(seed)
    signal = np.asarray(rng.standard_normal(int(duration_s * sr)) * 0.2, dtype=np.float64)
    delay = round(offset_ms / 1000.0 * sr)
    delayed = np.concatenate([np.zeros(delay, dtype=np.float64), signal])

    controls = [
        Control(
            name="a track against itself reads zero",
            expected_ms=0.0,
            measured_ms=measure_offset_ms(signal, signal, 5.0, sr=sr),
            tolerance_ms=tolerance_ms,
        ),
        Control(
            name=f"a pair built at {offset_ms:+.0f} ms reads that",
            expected_ms=offset_ms,
            measured_ms=measure_offset_ms(signal, delayed, 5.0, sr=sr),
            tolerance_ms=tolerance_ms,
        ),
        Control(
            name=f"the same pair measured {RESTART_S:.0f} s later reads the same",
            expected_ms=offset_ms,
            measured_ms=measure_offset_ms(signal, delayed, 5.0 + RESTART_S, sr=sr),
            tolerance_ms=tolerance_ms,
            note="a decoder landing elsewhere shows up here and nowhere else",
        ),
    ]
    # The data-shift control: move the material, not the window.
    extra_ms = 40.0
    extra = round(extra_ms / 1000.0 * sr)
    shifted = np.concatenate([np.zeros(delay + extra, dtype=np.float64), signal])
    controls.append(
        Control(
            name=f"moving the data by {extra_ms:+.0f} ms moves the answer",
            expected_ms=offset_ms + extra_ms,
            measured_ms=measure_offset_ms(signal, shifted, 5.0, sr=sr),
            tolerance_ms=tolerance_ms,
            note="an asymmetric search would read this as unchanged",
        ),
    )
    report = ControlReport(controls=tuple(controls))
    for line in report.describe():
        log.info("%s", line)
    return report


def file_controls(
    work_dir: Path | str,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
    offset_ms: float = KNOWN_OFFSET_MS,
    tolerance_ms: float = TOLERANCE_MS,
    duration_s: float = 30.0,
    sr: int = ANALYSIS_RATE,
) -> ControlReport:
    """The same questions, through the real programs and a real container.

    The arithmetic being right does not prove that reading a container hands
    the arithmetic what it thinks it does. This builds a file with a delay in
    it, decodes it the way every measurement here decodes, and asks the same
    questions of the result.

    The delay is a **container** delay, put there by the muxer rather than by
    adding silence, and that makes this control do more than repeat the array
    one. A raw decode starts at each stream's own first packet, so the delay is
    thrown away on the way out and the two dumps read as identical -- the exact
    failure :mod:`dubalign.epk_align` exists for. The correction is applied
    here, from the packet times, which is what turns the answer back into the
    250 ms the muxer was asked for. A pipeline that skipped it would pass three
    of these controls and be wrong about every real pair.
    """
    run = runner if runner is not None else default_runner(config)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    source = work / "control_source.mka"
    paired = work / "control_pair.mka"

    run(
        "ffmpeg",
        [
            "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"anoisesrc=color=white:seed=20260923:duration={duration_s:.3f}"
            f":sample_rate={sr}",
            "-c:a", "flac", str(source),
        ],
    )
    # The muxer's own delay switch builds the pair, so the known answer comes
    # from a different program than the one that measures it.
    run(
        "mkvmerge",
        ["-o", str(paired), str(source), "--sync", f"0:{offset_ms:.0f}", str(source)],
        ok=(0, 1),
    )
    dumps = []
    packets = []
    for index in (0, 1):
        raw = work / f"control_{index}.f32le"
        run(
            "ffmpeg",
            [
                "-nostdin", "-v", "error", "-y", "-i", str(paired),
                "-map", f"0:a:{index}", "-c:a", "pcm_f32le", "-ar", str(sr),
                "-ac", "1", "-f", "f32le", str(raw),
            ],
        )
        dumps.append(np.asarray(np.fromfile(raw, dtype=np.float32), dtype=np.float64))
        packets.append(first_packet_time(paired, index, runner=run))

    plain, delayed = dumps
    # What the decode threw away, put back. Without this every measurement
    # below reads zero, confidently, on a pair that is a quarter of a second
    # apart -- which is the whole point of measuring it here rather than only
    # on arrays.
    gap_ms = correct_lag(0.0, packets[0], packets[1]) * 1000.0

    def corrected(t: float) -> float | None:
        measured = measure_offset_ms(plain, delayed, t, sr=sr)
        return None if measured is None else measured + gap_ms
    controls = (
        Control(
            name="a decoded track against itself reads zero",
            expected_ms=0.0,
            measured_ms=measure_offset_ms(plain, plain, 5.0, sr=sr),
            tolerance_ms=tolerance_ms,
        ),
        Control(
            name=f"a muxed pair at {offset_ms:+.0f} ms reads that",
            expected_ms=offset_ms,
            measured_ms=corrected(5.0),
            tolerance_ms=tolerance_ms,
            note="a container delay that the raw decode discards",
        ),
        Control(
            name=f"the same pair measured {RESTART_S:.0f} s later reads the same",
            expected_ms=offset_ms,
            measured_ms=corrected(5.0 + RESTART_S),
            tolerance_ms=tolerance_ms,
        ),
        Control(
            name="the whole-timeline map agrees with the point measurements",
            expected_ms=offset_ms,
            measured_ms=(
                dense_map(plain, delayed, sr=sr, window_s=5.0, step_s=5.0)
                .confident()
                .median_lag_s()
                * 1000.0
                + gap_ms
            ),
            tolerance_ms=max(tolerance_ms, 2.0),
            note="the envelope search resolves a millisecond, not a sample",
        ),
    )
    report = ControlReport(controls=controls)
    for line in report.describe():
        log.info("%s", line)
    return report
