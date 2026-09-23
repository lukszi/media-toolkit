"""mkvkit.langid.ladder -- aggregation, the settle bar, and the escalation ladder.

This is where evidence becomes a decision, and it is deliberately the only
place. Everything here is a pure function over probability vectors: no audio,
no model, no file. That is what makes the ladder testable, re-runnable against
evidence collected months ago, and re-tunable without another pass over the
library.

**Aggregation.** Window probabilities are combined by summing log-probabilities
and reporting the geometric mean. An arithmetic mean lets eight complacent
windows outvote one window that is certain the track is something else; the
geometric mean collapses as soon as a single window strongly disagrees. That
is the intended behaviour: a track two windows disagree about should not
settle, it should escalate.

**The bar is asymmetric, and that is the main idea.** Confirming a tag that is
already in the file is cheap and writes nothing. Overturning one is a claim
that a person or a muxer got it wrong, and it rewrites metadata: it must clear
a higher confidence, more counted windows and a higher agreement fraction.
Symmetrical bars are how a language pass produces confident nonsense.

**Three rules use context, and none of them can settle a track alone.**

*Rule A, tag-confirm.* An existing tag that any counted window agrees with is
confirmed. Tags are usually right, and a window that happens to agree with a
wrong tag is rare -- so a single agreeing window is enough to confirm, and
confirming writes nothing.

*Rule B, prior-backed.* A strong sibling or release-name prior that agrees
with the audio winner lowers the audio bar somewhat. It never settles by
itself, it never applies against the audio, and it is blocked entirely for
the material where context is systematically misleading (see
:mod:`mkvkit.langid.priors`).

*Rule C, trimmed.* A film with one scene in another language has one window
that legitimately disagrees, and the geometric mean will -- correctly --
refuse to settle it. Rule C drops the windows least favourable to the winner
and re-aggregates, but only with corroboration from two independent stages
(a transcription cross-check and a different model family) and only when the
untrimmed agreement was already high. Without those conditions it is a
licence to discard inconvenient evidence.

**The numbers are defaults, not universals.** Every constant in
:class:`SettleBar` is a fitted default, see ``docs/methods/langid-ladder.md``.
They are configuration, the method for re-fitting them is in the method
document, and anyone reporting results with different values is not wrong --
they are reporting a different fit.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from ..langcodes import NO_CONTENT, canonical
from .priors import Prior

__all__ = [
    "Decision",
    "Posterior",
    "SettleBar",
    "StageBar",
    "Support",
    "Verdict",
    "aggregate",
    "mixed_split",
    "settle",
    "trimmed_aggregate",
    "unsettled_reasons",
]

#: Nothing is ever exactly zero after a softmax, and a zero would take the
#: logarithm with it. This floor is what a language the detector did not list
#: is worth.
PROBABILITY_FLOOR = 1e-6


class Verdict(Enum):
    """What was concluded, and therefore what may be done about it.

    ``CONFIRM`` and ``UNSETTLED`` write nothing. ``SETTLE`` and ``OVERTURN``
    are both writes and are kept apart because they are not equally safe:
    filling in a missing tag is a different act from contradicting one that is
    already there, and a report that lumps them together hides the only line
    worth reviewing by hand.
    """

    CONFIRM = "confirm"      # the existing tag stands; nothing to write
    SETTLE = "settle"        # no usable tag, and the evidence establishes one
    OVERTURN = "overturn"    # an existing tag is contradicted
    MIXED = "mixed"          # two languages genuinely split the track
    ZXX = "zxx"              # no linguistic content at all
    UNSETTLED = "unsettled"  # escalate, or ask a person

    @property
    def writes(self) -> bool:
        return self in (Verdict.SETTLE, Verdict.OVERTURN, Verdict.ZXX)


@dataclass(frozen=True)
class StageBar:
    """The bar one corroborating stage has to clear on its own terms."""

    conf: float
    margin: float
    counted: int = 0
    agree_frac: float = 0.0


@dataclass(frozen=True)
class SettleBar:
    """Every threshold in one object, so all of them are configuration.

    Defaults are fitted ones. See the module docstring before treating
    any of them as a universal.
    """

    # the standard bar: enough windows, confident, clearly ahead, and agreed
    min_counted: int = 5
    min_conf: float = 0.92
    min_margin: float = 0.50
    min_agree_frac: float = 0.80

    # contradicting a tag that already exists costs more
    override_conf: float = 0.97
    override_counted: int = 8
    override_agree_frac: float = 0.90

    # an item too short to yield the windows we would like: take what exists,
    # demand unanimity among them, and raise the confidence instead
    short_item_conf: float = 0.95
    min_counted_floor: int = 3

    # rule A -- tag-confirm
    confirm_min_counted: int = 1
    confirm_min_agree: float = 0.50

    # rule B -- a prior that agrees lowers the bar, by how much depends on how
    # strong the prior is. Never applies without audio.
    prior_strong: StageBar = StageBar(conf=0.85, margin=0.40, counted=3, agree_frac=0.80)
    prior_medium: StageBar = StageBar(conf=0.90, margin=0.40, counted=4, agree_frac=0.80)

    # rule C -- the trimmed aggregate, and what it costs to be allowed to use it
    trim_frac: float = 0.20
    trim_min_counted: int = 5
    trim_min_agree: float = 0.75
    trim_override_conf: float = 0.97
    trim_override_counted: int = 8
    trim_override_agree: float = 0.85

    # corroborating stages: a text cross-check and an independent model family
    text_bar: StageBar = StageBar(conf=0.80, margin=0.25, counted=3)
    family_bar: StageBar = StageBar(conf=0.70, margin=0.15, agree_frac=0.60)

    # "there is no speech in this at all"
    zxx_speech_s: float = 60.0
    window_min_speech_s: float = 8.0

    # two languages splitting the windows between them
    mixed_min_windows: int = 3
    mixed_min_frac: float = 0.25


@dataclass(frozen=True)
class Posterior:
    """The aggregate of a set of windows."""

    ranked: tuple[tuple[str, float], ...] = ()
    conf: float = 0.0
    margin: float = 0.0
    agree_frac: float = 0.0
    counted: int = 0
    trimmed: int = 0

    @property
    def winner(self) -> str | None:
        return self.ranked[0][0] if self.ranked else None


@dataclass(frozen=True)
class Support:
    """Corroboration from the later, more expensive stages of the ladder."""

    text_confirmed: bool = False
    family_agreed: bool = False
    speech_s: float | None = None
    stage: int = 1


@dataclass(frozen=True)
class Decision:
    """A verdict, the language it is about, and why -- in a reviewable form.

    ``rule`` names the rule that fired, so a report can be grouped by it and a
    disagreement can be argued about in terms of the rule rather than of the
    number. That is the difference between a pipeline somebody can audit and
    one they have to trust.
    """

    verdict: Verdict
    language: str | None = None
    rule: str = ""
    reason: str = ""
    posterior: Posterior = field(default_factory=Posterior)
    existing_tag: str | None = None
    stage: int = 1

    @property
    def writes(self) -> bool:
        return self.verdict.writes


# ----------------------------------------------------------------- aggregation
def aggregate(vectors: Sequence[Mapping[str, float]], *, top: int = 5) -> Posterior:
    """Combine window vectors into one posterior, as a geometric mean.

    The geometric mean rather than the arithmetic one, because one window that
    is certain of something else should be able to sink a verdict:

        >>> agreeing = [{"eng": 0.99, "deu": 0.01}] * 4
        >>> aggregate(agreeing).conf > 0.95
        True
        >>> aggregate([*agreeing, {"eng": 0.001, "deu": 0.999}]).conf < 0.5
        True
    """
    if not vectors:
        return Posterior()
    languages: set[str] = set()
    for vector in vectors:
        languages |= set(vector)
    count = len(vectors)
    scores: dict[str, float] = {}
    for language in languages:
        total = sum(
            math.log(max(vector.get(language, 0.0), PROBABILITY_FLOOR))
            for vector in vectors
        )
        scores[language] = math.exp(total / count)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    winner = ranked[0][0]
    conf = ranked[0][1]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    agreed = sum(
        1 for vector in vectors
        if vector and max(vector, key=lambda k: vector[k]) == winner
    )
    return Posterior(
        ranked=tuple((lang, round(score, 6)) for lang, score in ranked[:top]),
        conf=round(conf, 4),
        margin=round(conf - runner_up, 4),
        agree_frac=round(agreed / count, 3),
        counted=count,
    )


def trimmed_aggregate(
    vectors: Sequence[Mapping[str, float]], *, frac: float = 0.20, min_counted: int = 5
) -> Posterior:
    """Rule C's aggregate: drop the windows least favourable to the winner.

    Which windows are dropped is decided by the *untrimmed* winner, so trimming
    can never change who the winner is -- only how confidently the remainder
    supports them. A trim that could also elect a different language would be a
    search for a language the evidence likes, which is the opposite of what
    this is for.
    """
    base = aggregate(vectors)
    winner = base.winner
    drop = int(len(vectors) * frac)
    if not winner or drop == 0 or len(vectors) < min_counted:
        return base
    kept = sorted(vectors, key=lambda v: -v.get(winner, 0.0))[: len(vectors) - drop]
    return replace(aggregate(kept), trimmed=drop)


def mixed_split(
    vectors: Sequence[Mapping[str, float]], bar: SettleBar
) -> tuple[str, str] | None:
    """Two languages that consistently split the windows between them.

    A dual-language track, or a film whose second half is dubbed. It is a
    conclusion in its own right, not a failure to settle -- and importantly it
    is not something a single tag can express, so it never becomes a write.
    """
    if len(vectors) < 2 * bar.mixed_min_windows:
        return None
    votes = Counter(
        max(vector, key=lambda k: vector[k]) for vector in vectors if vector
    )
    if len(votes) < 2:
        return None
    (first, first_n), (second, second_n) = votes.most_common(2)
    total = len(vectors)
    if (
        first_n >= bar.mixed_min_windows
        and second_n >= bar.mixed_min_windows
        and first_n / total >= bar.mixed_min_frac
        and second_n / total >= bar.mixed_min_frac
    ):
        return (first, second)
    return None


# ---------------------------------------------------------------- the settle bar
def _clears(
    post: Posterior, *, conf: float, margin: float, counted: int, agree: float
) -> bool:
    return (
        post.counted >= counted
        and post.conf >= conf
        and post.margin >= margin
        and post.agree_frac >= agree
    )


def _describe(
    post: Posterior, *, conf: float, margin: float, counted: int, agree: float
) -> str:
    return (
        f"counted {post.counted}>={counted}, conf {post.conf:.3f}>={conf:.2f}, "
        f"margin {post.margin:.3f}>={margin:.2f}, agree {post.agree_frac:.2f}>={agree:.2f}"
    )


def settle(
    post: Posterior,
    *,
    existing_tag: str | None = None,
    bar: SettleBar | None = None,
    prior: Prior | None = None,
    support: Support | None = None,
    max_windows: int | None = None,
    mixed: tuple[str, str] | None = None,
    trimmed: Posterior | None = None,
) -> Decision:
    """Decide what this evidence supports, and say which rule decided it.

    The order is not arbitrary. "No speech at all" comes first because it makes
    every other question moot. A mixed track is next, because a mixed track
    that clears the bar for its majority language would otherwise be settled as
    that language, which is wrong in a way nobody notices. Then the existing
    tag, then the audio, then the two context rules -- each of which can only
    ever *lower* a bar the audio nearly cleared.
    """
    bar = bar or SettleBar()
    support = support or Support()
    tag = canonical(existing_tag)
    winner = post.winner

    # 1. no linguistic content: an answer, not an absence of one
    if support.speech_s is not None and support.speech_s < bar.zxx_speech_s:
        return Decision(
            Verdict.ZXX, NO_CONTENT, "zxx.no-speech",
            f"{support.speech_s:.0f}s of speech in the whole track, under "
            f"{bar.zxx_speech_s:.0f}s",
            post, tag, support.stage,
        )

    if winner is None:
        return Decision(
            Verdict.UNSETTLED, None, "unsettled.no-evidence",
            "no usable windows", post, tag, support.stage,
        )

    # 2. two languages sharing the track
    if mixed is not None:
        return Decision(
            Verdict.MIXED, None, "mixed.split",
            f"windows split between {mixed[0]} and {mixed[1]}", post, tag, support.stage,
        )

    # 3. rule A -- the existing tag agrees with the audio
    if tag is not None and tag == winner:
        if post.counted >= bar.confirm_min_counted and \
                post.agree_frac >= bar.confirm_min_agree:
            return Decision(
                Verdict.CONFIRM, tag, "A.tag-confirm",
                f"the tag agrees with {post.counted} counted window(s), "
                f"agreement {post.agree_frac:.2f}",
                post, tag, support.stage,
            )

    overturning = tag is not None and tag != winner
    need_conf = bar.override_conf if overturning else bar.min_conf
    need_counted = bar.override_counted if overturning else bar.min_counted
    need_agree = bar.override_agree_frac if overturning else bar.min_agree_frac
    label = "override" if overturning else "standard"

    # an item that cannot supply the windows we want: unanimity, higher bar
    if max_windows is not None and max_windows < need_counted:
        need_counted = max(bar.min_counted_floor, max_windows) if max_windows else 1
        need_counted = min(need_counted, max(1, max_windows))
        need_conf = max(need_conf, bar.short_item_conf)
        need_agree = 1.0
        label += "/short-item"

    verdict = Verdict.OVERTURN if overturning else Verdict.SETTLE

    # 4. the audio on its own
    if _clears(post, conf=need_conf, margin=bar.min_margin,
               counted=need_counted, agree=need_agree):
        return Decision(
            verdict, winner, f"audio.{label}",
            _describe(post, conf=need_conf, margin=bar.min_margin,
                      counted=need_counted, agree=need_agree),
            post, tag, support.stage,
        )

    # 5. rule B -- a prior that agrees lowers the bar. Never the other way.
    if prior is not None and prior and prior.language == winner:
        stage_bar = bar.prior_strong if prior.strength == "strong" else bar.prior_medium
        if _clears(post, conf=stage_bar.conf, margin=stage_bar.margin,
                   counted=stage_bar.counted, agree=stage_bar.agree_frac):
            return Decision(
                verdict, winner, f"B.prior-{prior.strength}",
                _describe(post, conf=stage_bar.conf, margin=stage_bar.margin,
                          counted=stage_bar.counted, agree=stage_bar.agree_frac)
                + f"; prior from {', '.join(prior.sources) or 'context'}",
                post, tag, support.stage,
            )

    # 6. rule C -- the trimmed aggregate, and only with both corroborations
    if trimmed is not None and trimmed.winner == winner \
            and support.text_confirmed and support.family_agreed \
            and post.agree_frac >= bar.trim_min_agree \
            and post.counted >= bar.trim_min_counted:
        trim_conf = bar.trim_override_conf if overturning else bar.min_conf
        trim_counted = bar.trim_override_counted if overturning else bar.min_counted
        trim_agree = bar.trim_override_agree if overturning else bar.min_agree_frac
        if _clears(trimmed, conf=trim_conf, margin=bar.min_margin,
                   counted=trim_counted - trimmed.trimmed, agree=trim_agree):
            return Decision(
                verdict, winner, "C.trimmed",
                f"{trimmed.trimmed} window(s) trimmed; "
                + _describe(trimmed, conf=trim_conf, margin=bar.min_margin,
                            counted=trim_counted - trimmed.trimmed, agree=trim_agree)
                + "; text and cross-family corroboration present",
                post, tag, support.stage,
            )

    return Decision(
        Verdict.UNSETTLED, winner, f"unsettled.{label}",
        _describe(post, conf=need_conf, margin=bar.min_margin,
                  counted=need_counted, agree=need_agree),
        post, tag, support.stage,
    )


def unsettled_reasons(
    post: Posterior,
    *,
    existing_tag: str | None = None,
    bar: SettleBar | None = None,
    speech_windows: int | None = None,
) -> tuple[str, ...]:
    """Why a track did not settle, as short labels a report can group by.

    Counting these across a run is how the ladder gets tuned: a queue that is
    mostly ``fewspeech`` needs more windows, one that is mostly ``lowmargin``
    needs a better detector, and one that is mostly ``tagdisagree`` needs a
    person.
    """
    bar = bar or SettleBar()
    reasons: list[str] = []
    if not post.ranked:
        return ("noevidence",)
    if post.conf < bar.min_conf:
        reasons.append("lowconf")
    if post.margin < bar.min_margin:
        reasons.append("lowmargin")
    if post.agree_frac < bar.min_agree_frac:
        reasons.append("lowagreement")
    if post.counted < bar.min_counted:
        reasons.append("fewwindows")
    if speech_windows is not None and speech_windows == 0:
        reasons.append("nospeech")
    tag = canonical(existing_tag)
    if tag is not None and post.winner is not None and tag != post.winner:
        reasons.append("tagdisagree")
    return tuple(reasons)
