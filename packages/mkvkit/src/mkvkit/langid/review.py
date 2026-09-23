"""mkvkit.langid.review -- turn collected evidence into decisions, and a queue.

The step between "we listened to it" and "here is what we propose". It is a
separate module from both the listening and the reporting for one reason: it
must be runnable *without* either. Re-deciding a library after changing a
threshold should cost a second and read nothing but a JSONL file. If deciding
requires the audio, a threshold can only be tuned by another pass over
terabytes, which means it never gets tuned.

Two products come out of here.

**Outcomes** -- one per track, carrying the evidence, the verdict, the rule
that produced it and the tag it is about. A report groups them; an apply step
filters them to the ones that write; a person reads the ones that do not
settle.

**The re-scan queue** -- the tracks worth another look, and *why*, so the
second pass can do something different rather than the same thing again.
A track whose windows were all music does not need a better model, it needs
different windows. A track two windows disagree about does not need different
windows, it needs a person. Sending both back through the same pass is how a
queue stops shrinking.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .ladder import (
    Decision,
    Posterior,
    SettleBar,
    Support,
    Verdict,
    aggregate,
    mixed_split,
    settle,
    trimmed_aggregate,
    unsettled_reasons,
)
from .priors import Prior
from .worker import TrackEvidence, max_windows_for

__all__ = [
    "Outcome",
    "Rescan",
    "RescanReason",
    "decide",
    "decide_all",
    "rescan_queue",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """One track's evidence and what was concluded from it."""

    evidence: TrackEvidence
    decision: Decision
    prior: Prior | None = None
    reasons: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.evidence.path

    @property
    def settled(self) -> bool:
        return self.decision.verdict is not Verdict.UNSETTLED

    @property
    def writes(self) -> bool:
        return self.decision.writes


def decide(
    evidence: TrackEvidence,
    *,
    bar: SettleBar | None = None,
    prior: Prior | None = None,
    support: Support | None = None,
    window_s: float = 20.0,
) -> Outcome:
    """Aggregate one track's windows and apply the settle rules to them.

    Only the counted windows are aggregated: a window with no speech in it
    carries a language guess about music, and averaging that in is how a
    silent track acquires an opinion.
    """
    bar = bar or SettleBar()
    vectors = [window.probabilities for window in evidence.counted]
    posterior: Posterior = aggregate(vectors)
    trimmed = trimmed_aggregate(
        vectors, frac=bar.trim_frac, min_counted=bar.trim_min_counted
    )
    decision = settle(
        posterior,
        existing_tag=evidence.existing_tag,
        bar=bar,
        prior=prior,
        support=support,
        max_windows=max_windows_for(evidence.runtime_s, window_s),
        mixed=mixed_split(vectors, bar),
        trimmed=trimmed,
    )
    reasons = (
        unsettled_reasons(
            posterior, existing_tag=evidence.existing_tag, bar=bar,
            speech_windows=len(evidence.counted),
        )
        if decision.verdict is Verdict.UNSETTLED
        else ()
    )
    return Outcome(evidence=evidence, decision=decision, prior=prior, reasons=reasons)


def decide_all(
    evidence: Iterable[TrackEvidence],
    *,
    bar: SettleBar | None = None,
    priors: dict[tuple[str, int], Prior] | None = None,
    window_s: float = 20.0,
) -> Iterator[Outcome]:
    priors = priors or {}
    for record in evidence:
        yield decide(
            record, bar=bar,
            prior=priors.get((record.path, record.stream_index)),
            window_s=window_s,
        )


class RescanReason:
    """Why a track is going round again -- and therefore what to change.

    These are not severities. They are instructions: each one names a
    different second pass, and a queue that does not distinguish them repeats
    the first pass and gets the first answer.
    """

    #: every window was music or silence: sample elsewhere in the runtime
    OTHER_WINDOWS = "other-windows"
    #: too few windows counted: sample more of them, or the whole track
    MORE_WINDOWS = "more-windows"
    #: confident but close: a transcription cross-check may separate them
    CROSS_CHECK = "cross-check"
    #: the audio contradicts the existing tag without clearing the higher bar
    NEEDS_A_PERSON = "needs-a-person"
    #: the read failed: try again before concluding anything about the content
    READ_FAILED = "read-failed"


@dataclass(frozen=True)
class Rescan:
    """One queued track, the reason, and the stage that should handle it."""

    path: str
    stream_index: int
    reason: str
    stage: int = 2
    detail: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def rescan_queue(
    outcomes: Sequence[Outcome], *, bar: SettleBar | None = None
) -> list[Rescan]:
    """The tracks worth another pass, each labelled with what to do differently.

    Ordered by what the next pass costs: re-sampling first, because it is
    cheap and settles most of what is left; a person last, because their time
    is the scarcest thing in the pipeline and every earlier stage exists to
    spend less of it.
    """
    bar = bar or SettleBar()
    queued: list[Rescan] = []
    for outcome in outcomes:
        evidence, decision = outcome.evidence, outcome.decision
        if decision.verdict is not Verdict.UNSETTLED:
            continue
        if evidence.error and not evidence.windows:
            queued.append(Rescan(
                evidence.path, evidence.stream_index, RescanReason.READ_FAILED,
                stage=1, detail=evidence.error,
            ))
            continue
        if evidence.no_speech:
            queued.append(Rescan(
                evidence.path, evidence.stream_index, RescanReason.OTHER_WINDOWS,
                stage=2, detail="every window was music or silence",
            ))
            continue
        if "fewwindows" in outcome.reasons:
            queued.append(Rescan(
                evidence.path, evidence.stream_index, RescanReason.MORE_WINDOWS,
                stage=2, detail=f"{len(evidence.counted)} counted window(s)",
            ))
            continue
        if "tagdisagree" in outcome.reasons:
            queued.append(Rescan(
                evidence.path, evidence.stream_index, RescanReason.NEEDS_A_PERSON,
                stage=5,
                detail=f"tag {evidence.existing_tag} against "
                       f"{decision.posterior.winner} at {decision.posterior.conf:.2f}",
            ))
            continue
        queued.append(Rescan(
            evidence.path, evidence.stream_index, RescanReason.CROSS_CHECK,
            stage=3, detail=", ".join(outcome.reasons) or decision.reason,
            notes=outcome.reasons,
        ))
    queued.sort(key=lambda item: (item.stage, item.path, item.stream_index))
    return queued
