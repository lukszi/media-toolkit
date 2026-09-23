"""dubalign.plan -- the splice plan as reviewable data, and where the joins go.

The design idea worth publishing is the small one: **a plan is a document, not
a list of constants in a script.** It can be read, diffed, argued with, edited
by hand and run again. A join that landed badly is one number in a file rather
than an edit to a program, and two builds of the same programme differ by a
diff somebody can look at.

The rules the placement follows were all learned the expensive way.

**A join goes on the jump.** Not near it, not at the next convenient pause --
on it. A join placed a second away from the jump it renders leaves the entire
step uncorrected for that second, and a join several seconds away leaves a
step-sized error running for all of them. Nobody hears it as a click,
because it is not a click: it is a whole passage out of sync. So the search
bracket is narrow, and it is *narrower the bigger the step is* -- a large error
must not be allowed to wait for a nice pause, while a small one can.

**Score the span that actually moves.** A join that closes a step of S
milliseconds repeats or drops S milliseconds of material. Scoring the
instantaneous level at the join instead is the mistake that produced an audible
doubled syllable: inside continuous speech the quietest instant is the gap
between two consonants, so the join lands mid-word and repeats the word's own
first syllable, at full level. What must be quiet is the span that gets
repeated or dropped, over its whole length.

**Short, equal-power crossfades.** Twenty milliseconds, sine and cosine. A
linear fade dips in the middle because two uncorrelated signals add in power,
not in amplitude; a long fade is audible as a swell even where the alignment is
perfect.

**A step cannot be rate-ramped away cheaply.** Absorbing a step of S by
stretching the material around it costs at least S/2 of error somewhere, spread
out instead of concentrated. That was built and rejected on measurement: the
variant made the local error at the joins worse, not better. A clean join at
the right instant beats a smooth error everywhere.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import itertools
import logging
import math
import tomllib
from dataclasses import dataclass, replace

import numpy as np

from .align import ANALYSIS_RATE, FULL_RATE, Signal
from .changepoints import Segmentation

__all__ = [
    "BIG_STEP_S",
    "BRACKET_BIG_S",
    "BRACKET_SMALL_S",
    "CROSSFADE_HALF_WIDTH_S",
    "PlanError",
    "Seam",
    "SeamScore",
    "Segment",
    "SplicePlan",
    "place_seam",
    "plan_from_measurements",
    "span_level_db",
]

log = logging.getLogger(__name__)

#: Half the crossfade: twenty milliseconds in total.
CROSSFADE_HALF_WIDTH_S = 0.010
#: Above this a step is "big" and its join is kept close to its jump.
BIG_STEP_S = 0.040
#: How far from the jump a join may be placed, for a big step and a small one.
BRACKET_BIG_S = 1.0
BRACKET_SMALL_S = 3.0
#: How far apart candidate join positions are considered.
CANDIDATE_STEP_S = 0.005
#: The shortest span that is scored, even for a tiny step.
MIN_SPAN_S = 0.030
#: How much material either side is used as the loudness the join is judged
#: against. A join is good when the span it moves is far below its context.
CONTEXT_S = 2.0


class PlanError(ValueError):
    """The plan does not describe something that can be built."""


# ------------------------------------------------------------------ the parts
@dataclass(frozen=True)
class Seam:
    """Where one segment gives way to the next, and why it is there."""

    t: float
    half_width_s: float = CROSSFADE_HALF_WIDTH_S
    reason: str = ""
    #: What the measurement said, so a reader can see how far the join moved.
    jump_t: float | None = None
    step_s: float = 0.0
    #: How far below its surroundings the moved span sits, in decibels. This is
    #: the number that says whether the join will be heard.
    margin_db: float = 0.0

    @property
    def distance_from_jump_s(self) -> float:
        return 0.0 if self.jump_t is None else abs(self.t - self.jump_t)

    def describe(self) -> str:
        where = (
            "" if self.jump_t is None
            else f", {self.distance_from_jump_s * 1000:.0f} ms from the jump"
        )
        return (
            f"{self.t:10.4f} s  +/-{self.half_width_s * 1000:.0f} ms  "
            f"step {self.step_s * 1000:+.1f} ms  margin {self.margin_db:+.1f} dB"
            f"{where}  {self.reason}"
        )


@dataclass(frozen=True)
class Segment:
    """One stretch of output, and where in a source it comes from.

    The source position of output frame ``n`` is

        ``anchor_frame + offset_samples + (n - anchor_frame) * rate_ratio``

    which carries the offset and the rate difference in one expression -- so a
    stretch that is both shifted and sliding is built with a single pass over
    the material rather than a shift followed by a resampling.
    """

    label: str
    source: str
    offset_samples: int
    rate_ratio: float = 1.0
    anchor_frame: int = 0
    gain_db: float = 0.0

    @property
    def drifts(self) -> bool:
        return self.rate_ratio != 1.0

    def position(self, frames: Signal) -> Signal:
        base = float(self.anchor_frame + self.offset_samples)
        return np.asarray(
            base + (frames - self.anchor_frame) * self.rate_ratio, dtype=np.float64
        )

    def describe(self) -> str:
        drift = "" if not self.drifts else f"  rate {self.rate_ratio:.8f}"
        gain = "" if not self.gain_db else f"  gain {self.gain_db:+.2f} dB"
        return (
            f"{self.label:<28} {self.source:<10} offset {self.offset_samples:+10d}"
            f"{drift}{gain}"
        )


@dataclass(frozen=True)
class SplicePlan:
    """Everything needed to build one track, in a form a person can read."""

    sample_rate: int
    channels: int
    total_frames: int
    seams: tuple[Seam, ...]
    segments: tuple[Segment, ...]
    reference: str = ""
    note: str = ""

    @property
    def duration_s(self) -> float:
        return self.total_frames / self.sample_rate

    def bounds(self) -> list[tuple[int, int]]:
        """The output frame range each segment owns, split at the seams."""
        edges = [0, *(round(s.t * self.sample_rate) for s in self.seams), self.total_frames]
        return list(itertools.pairwise(edges))

    # ------------------------------------------------------------- self-check
    def problems(self) -> list[str]:
        """Everything wrong with this plan, all of it, before anything is built.

        A plan is cheap to check and expensive to build. Every rule here is one
        that has produced a file somebody had to throw away.
        """
        found: list[str] = []
        if self.sample_rate <= 0 or self.channels <= 0 or self.total_frames <= 0:
            found.append("the output has no rate, no channels or no length")
        if len(self.segments) != len(self.seams) + 1:
            found.append(
                f"{len(self.segments)} segment(s) need {len(self.segments) - 1} seam(s), "
                f"not {len(self.seams)}"
            )
        previous = 0.0
        for index, seam in enumerate(self.seams, 1):
            if seam.t <= previous:
                found.append(f"seam {index} at {seam.t:.4f} s is not after the one before")
            if not 0.0 < seam.t < self.duration_s:
                found.append(f"seam {index} at {seam.t:.4f} s is outside the output")
            if seam.half_width_s <= 0:
                found.append(f"seam {index} has no crossfade")
            if seam.t - seam.half_width_s < previous:
                found.append(f"seam {index}'s crossfade reaches into the seam before it")
            previous = seam.t + seam.half_width_s
        for segment in self.segments:
            if not 0.5 < segment.rate_ratio < 2.0:
                found.append(
                    f"{segment.label}: a rate ratio of {segment.rate_ratio} is not a "
                    "drift correction, it is a different programme"
                )
        return found

    def check(self) -> None:
        problems = self.problems()
        if problems:
            raise PlanError("; ".join(problems))

    def describe(self) -> list[str]:
        lines = [
            f"{self.total_frames} frames = {self.duration_s:.3f} s at "
            f"{self.sample_rate} Hz, {self.channels} channel(s)",
            f"{len(self.segments)} segment(s), {len(self.seams)} seam(s)",
        ]
        for segment, (start, end) in zip(self.segments, self.bounds(), strict=True):
            lines.append(
                f"  {start / self.sample_rate:9.3f} - {end / self.sample_rate:9.3f} s  "
                f"{segment.describe()}"
            )
        lines += [f"  seam at {seam.describe()}" for seam in self.seams]
        return lines

    # ------------------------------------------------------------- the document
    def to_toml(self) -> str:
        """Write the plan out. Reviewable, diffable, editable, re-runnable."""
        lines = [
            "# A splice plan. Edit it and run it again: every number here is",
            "# either a measurement or a decision, and both are meant to be read.",
            "",
            f"sample_rate = {self.sample_rate}",
            f"channels = {self.channels}",
            f"total_frames = {self.total_frames}",
        ]
        if self.reference:
            lines.append(f"reference = {_quote(self.reference)}")
        if self.note:
            lines.append(f"note = {_quote(self.note)}")
        for segment in self.segments:
            lines += [
                "",
                "[[segment]]",
                f"label = {_quote(segment.label)}",
                f"source = {_quote(segment.source)}",
                f"offset_samples = {segment.offset_samples}",
                f"rate_ratio = {segment.rate_ratio!r}",
                f"anchor_frame = {segment.anchor_frame}",
                f"gain_db = {segment.gain_db!r}",
            ]
        for seam in self.seams:
            lines += [
                "",
                "[[seam]]",
                f"t = {seam.t!r}",
                f"half_width_s = {seam.half_width_s!r}",
                f"step_s = {seam.step_s!r}",
                f"margin_db = {seam.margin_db!r}",
                f"reason = {_quote(seam.reason)}",
            ]
            if seam.jump_t is not None:
                lines.append(f"jump_t = {seam.jump_t!r}")
        return "\n".join(lines) + "\n"

    @classmethod
    def from_toml(cls, text: str) -> SplicePlan:
        """Read a plan back, including one a person edited by hand."""
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise PlanError(f"the plan is not readable: {exc}") from exc
        missing = [k for k in ("sample_rate", "channels", "total_frames") if k not in raw]
        if missing:
            raise PlanError(f"the plan is missing {', '.join(missing)}")
        segments = tuple(
            Segment(
                label=str(s.get("label", "")),
                source=str(s["source"]),
                offset_samples=int(s.get("offset_samples", 0)),
                rate_ratio=float(s.get("rate_ratio", 1.0)),
                anchor_frame=int(s.get("anchor_frame", 0)),
                gain_db=float(s.get("gain_db", 0.0)),
            )
            for s in raw.get("segment", [])
        )
        seams = tuple(
            Seam(
                t=float(s["t"]),
                half_width_s=float(s.get("half_width_s", CROSSFADE_HALF_WIDTH_S)),
                reason=str(s.get("reason", "")),
                jump_t=None if s.get("jump_t") is None else float(s["jump_t"]),
                step_s=float(s.get("step_s", 0.0)),
                margin_db=float(s.get("margin_db", 0.0)),
            )
            for s in raw.get("seam", [])
        )
        return cls(
            sample_rate=int(raw["sample_rate"]),
            channels=int(raw["channels"]),
            total_frames=int(raw["total_frames"]),
            seams=seams,
            segments=segments,
            reference=str(raw.get("reference", "")),
            note=str(raw.get("note", "")),
        )


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------- the placement
def span_level_db(x: Signal, t0: float, t1: float, sr: int) -> float:
    """The level of one stretch, in decibels below full scale."""
    start, end = max(0, round(t0 * sr)), min(len(x), round(t1 * sr))
    if end - start < 1:
        return float("-inf")
    block = x[start:end]
    rms = float(np.sqrt(np.mean(block * block)))
    return 20.0 * math.log10(rms) if rms > 0 else float("-inf")


@dataclass(frozen=True)
class SeamScore:
    """One candidate position for a join, and what it would cost."""

    t: float
    span_db: float
    context_db: float

    @property
    def margin_db(self) -> float:
        """How far the moved span sits below its surroundings. More is better."""
        return self.context_db - self.span_db


def place_seam(
    other: Signal,
    jump_t: float,
    step_s: float,
    *,
    sr: int = ANALYSIS_RATE,
    bracket_s: float | None = None,
    candidate_step_s: float = CANDIDATE_STEP_S,
    context_s: float = CONTEXT_S,
    min_span_s: float = MIN_SPAN_S,
) -> Seam:
    """Choose where the join goes, near the jump, in the quietest available span.

    The span scored is the material the join actually repeats or drops -- as
    long as the step, never shorter than ``min_span_s`` -- because that is what
    a listener hears. The bracket is deliberately tight around the jump, and
    tighter still for a large step: a join that waits for a pause leaves the
    whole step running until it gets there.
    """
    if bracket_s is None:
        bracket_s = BRACKET_BIG_S if abs(step_s) > BIG_STEP_S else BRACKET_SMALL_S
    span = max(abs(step_s), min_span_s)
    candidates: list[SeamScore] = []
    offset = -bracket_s
    while offset <= bracket_s + 1e-9:
        t = jump_t + offset
        if t - span / 2.0 >= 0.0:
            candidates.append(
                SeamScore(
                    t=t,
                    span_db=span_level_db(other, t - span / 2.0, t + span / 2.0, sr),
                    context_db=span_level_db(other, t - context_s, t + context_s, sr),
                )
            )
        offset += candidate_step_s
    if not candidates:
        return Seam(
            t=jump_t, reason="no room to move the join", jump_t=jump_t, step_s=step_s
        )
    best = min(candidates, key=lambda c: c.span_db)
    at_the_jump = min(candidates, key=lambda c: abs(c.t - jump_t))
    reason = (
        "on the jump"
        if best.t == at_the_jump.t
        else f"quietest span within {bracket_s:.1f} s of the jump"
    )
    log.info(
        "seam at %.4f s (%.0f ms from the jump), moved span %.1f dBFS, "
        "%.1f dB below its context",
        best.t, (best.t - jump_t) * 1000.0, best.span_db, best.margin_db,
    )
    return Seam(
        t=best.t,
        reason=reason,
        jump_t=jump_t,
        step_s=step_s,
        margin_db=best.margin_db,
    )


def plan_from_measurements(
    found: Segmentation,
    *,
    sample_rate: int = FULL_RATE,
    total_frames: int,
    other: Signal | None = None,
    analysis_rate: int = ANALYSIS_RATE,
    source: str = "other",
    channels: int = 1,
    reference: str = "reference",
    quiet_search: bool = True,
    half_width_s: float = CROSSFADE_HALF_WIDTH_S,
) -> SplicePlan:
    """Turn a measurement into a buildable plan.

    One segment per straight stretch and one seam per jump. Each segment
    carries the offset at its own start and the rate the source has to be read
    at, so a stretch that drifts is corrected by the same pass that copies it.

    With ``quiet_search`` and the source's analysis signal, each join is moved
    to the quietest span inside a tight bracket around its jump. Without them
    the joins sit exactly on the jumps, which is correct and occasionally
    audible.
    """
    if not found.segments:
        raise PlanError("there is nothing to plan: the measurement found no segments")
    seams: list[Seam] = []
    for change in found.changepoints:
        if quiet_search and other is not None:
            seams.append(
                replace(
                    place_seam(other, change.t, change.step_s, sr=analysis_rate),
                    half_width_s=half_width_s,
                )
            )
        else:
            seams.append(
                Seam(
                    t=change.t, half_width_s=half_width_s, reason="on the jump",
                    jump_t=change.t, step_s=change.step_s,
                )
            )

    edges = [0.0, *(s.t for s in seams), total_frames / sample_rate]
    segments: list[Segment] = []
    for index, (start, _end) in enumerate(itertools.pairwise(edges)):
        model = found.segments[min(index, len(found.segments) - 1)]
        anchor = round(start * sample_rate)
        segments.append(
            Segment(
                label=f"segment {index + 1}",
                source=source,
                offset_samples=round(model.lag_at(start) * sample_rate),
                rate_ratio=1.0 + model.slope,
                anchor_frame=anchor,
            )
        )
    plan = SplicePlan(
        sample_rate=sample_rate,
        channels=channels,
        total_frames=total_frames,
        seams=tuple(seams),
        segments=tuple(segments),
        reference=reference,
    )
    for line in plan.describe():
        log.info("%s", line)
    return plan
