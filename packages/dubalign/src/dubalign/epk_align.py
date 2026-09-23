"""dubalign.epk_align -- excerpt alignment that survives a leading gap.

This module exists because of one finding, and it is the finding that makes
the difference between a measurement that is right and one that is confidently
wrong by exactly the amount being measured.

**A raw decode starts at the stream's first packet, not at time zero.** When a
track's first packet sits, say, thirty milliseconds into a container, the raw
samples that come out start at that packet: sample zero of the dump is
thirty milliseconds into the timeline. Correlating two dumps whose streams
begin at different times therefore measures the difference between them *plus*
the difference in where they began, and nothing in the correlation can tell
those two apart. The result is a clean, high-confidence, wrong number.

The fix does not require knowing how any particular decoder handles a gap. It
requires not depending on it:

1. copy an excerpt of the container with its timestamps preserved, so both
   streams keep whatever head they had;
2. ask what each stream's first packet time actually is;
3. dump each stream to raw samples, where index zero is that stream's own
   first packet;
4. correlate, and correct the index-space answer by the difference:

       ``true_lag = index_lag + (first_packet[b] - first_packet[a])``

Nothing above assumes anything about seeking, about gap filling, or about what
the container declared. It reads what happened and subtracts it.

Two related traps are worth naming because they are in the same family. A seek
lands where the decoder can resume, which on some material is several seconds
from where it was asked -- so the excerpt is cut once with both streams in one
command, and both halves land in the same place whatever that place is. And a
track built to be sample-exact against another track's *decoded* samples has to
be muxed back with that track's own start time as its delay, or it arrives
early in the finished file by exactly the head that was invisible.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mkvkit.config import Config
from mkvkit.run import Runner, default_runner

from .align import ANALYSIS_RATE, best_with_ratio, ncc_full
from .probe import first_packet_time

__all__ = [
    "AbsoluteLag",
    "absolute_lag",
    "correct_lag",
    "excerpt_command",
]

log = logging.getLogger(__name__)

#: A little material before the point of interest, so the excerpt has settled
#: by the time the measurement window starts.
LEAD_IN_S = 0.6


@dataclass(frozen=True)
class AbsoluteLag:
    """What the correlation said, what the packets said, and the answer."""

    index_lag_s: float
    first_packet_a_s: float
    first_packet_b_s: float
    r: float
    ratio: float

    @property
    def packet_difference_s(self) -> float:
        return self.first_packet_b_s - self.first_packet_a_s

    @property
    def true_lag_s(self) -> float:
        return self.index_lag_s + self.packet_difference_s

    @property
    def true_lag_ms(self) -> float:
        return self.true_lag_s * 1000.0

    def describe(self) -> str:
        return (
            f"index lag {self.index_lag_s * 1000:+.3f} ms, packets differ by "
            f"{self.packet_difference_s * 1000:+.3f} ms -> true lag "
            f"{self.true_lag_ms:+.3f} ms (r={self.r:.3f}, peak/second={self.ratio:.2f})"
        )


def correct_lag(
    index_lag_s: float, first_packet_a_s: float, first_packet_b_s: float
) -> float:
    """``index_lag + (first_packet[b] - first_packet[a])``, and nothing else.

    One line, on its own, because it is the whole finding and because a sign
    error here is invisible in every other test: both answers look plausible
    and only one of them is right.
    """
    return index_lag_s + (first_packet_b_s - first_packet_a_s)


def excerpt_command(
    source: Path | str,
    out: Path | str,
    stream_a: int,
    stream_b: int,
    t0: float,
    t1: float,
) -> list[str]:
    """Copy both streams of one window, once, with their timestamps kept.

    Both streams in one command rather than two: whatever the seek does, it
    does the same thing to both, so an error in where it landed cancels instead
    of becoming the measurement.
    """
    if t1 <= t0:
        raise ValueError("the excerpt has no length")
    return [
        "-nostdin", "-v", "error", "-y",
        # timestamps preserved, so each stream keeps the head it had
        "-copyts", "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(source),
        "-map", f"0:{stream_a}", "-map", f"0:{stream_b}",
        "-c", "copy", str(out),
    ]


def absolute_lag(
    source: Path | str,
    stream_a: int,
    stream_b: int,
    t0: float,
    *,
    dur: float = 40.0,
    work_dir: Path | str | None = None,
    sr: int = ANALYSIS_RATE,
    search_s: float = 0.5,
    runner: Runner | None = None,
    config: Config | None = None,
) -> AbsoluteLag:
    """Measure two streams of one container against each other, honestly.

    ``stream_a`` and ``stream_b`` are container stream indices, not audio
    positions: the excerpt is cut by index and the two streams become audio 0
    and audio 1 of it, in that order.
    """
    run = runner if runner is not None else default_runner(config)
    work = Path(work_dir) if work_dir is not None else Path(source).parent
    work.mkdir(parents=True, exist_ok=True)
    excerpt = work / f"excerpt_{stream_a}_{stream_b}_{int(t0)}.mkv"
    run(
        "ffmpeg",
        excerpt_command(source, excerpt, stream_a, stream_b, max(0.0, t0 - LEAD_IN_S),
                        t0 + dur),
    )
    packets = [first_packet_time(excerpt, index, runner=run) for index in (0, 1)]

    dumps = []
    for index in (0, 1):
        raw = work / f"excerpt_{stream_a}_{stream_b}_{int(t0)}_{index}.f32le"
        run(
            "ffmpeg",
            [
                "-nostdin", "-v", "error", "-y", "-i", str(excerpt),
                "-map", f"0:a:{index}", "-c:a", "pcm_f32le", "-ar", str(sr),
                "-ac", "1", "-f", "f32le", str(raw),
            ],
        )
        dumps.append(np.asarray(np.fromfile(raw, dtype=np.float32), dtype=np.float64))

    a, b = dumps
    reach = int(search_s * sr)
    length = min(len(a), len(b)) - 2 * reach
    if length < sr:
        raise ValueError(
            f"the excerpt decoded to {min(len(a), len(b)) / sr:.2f} s, which is not "
            "enough to measure with this search width"
        )
    peak = best_with_ratio(
        ncc_full(a[reach : reach + length], b[: length + 2 * reach]),
        max(1, int(0.002 * sr)),
    )
    found = AbsoluteLag(
        index_lag_s=(peak.index - reach) / sr,
        first_packet_a_s=packets[0],
        first_packet_b_s=packets[1],
        r=peak.r,
        ratio=peak.ratio,
    )
    log.info("%s", found.describe())
    return found
