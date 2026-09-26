"""The owner's rules: which copy may stand in for which, and which one is kept.

Two questions, asked in this order and never mixed up.

**May this copy replace that one?** (:func:`coverage`) Only if nothing the
owner cares about is lost:

* every audio language of the other copy, at the same number of channels or
  more (commentary tracks aside); a language the policy lists as droppable
  may be lost, and the loss is reported;
* every lossless audio track, by language;
* every commentary track, by language;
* every subtitle language in ``policy.keep_languages`` that the other copy
  has, and every forced subtitle in those languages -- external subtitle
  files beside the video count. With no ``keep_languages`` configured every
  subtitle language the other copy has is kept;
* a running time no more than ``runtime_tolerance_s`` shorter.

What may be lost, and is reported rather than refused: subtitles in a
language the policy does not keep, and a second, lesser track in a language
the kept copy already has.

**Of the copies that may replace all the others, which one?**
(:func:`rank`) By ``policy.dedupe.prefer``, first criterion first: a lossless
track, then more channels, then source over re-encode, then the resolution
class (:func:`jfkit.dedupe.facts.resolution_class`: a scope crop is not a
lower resolution than the same picture letterboxed), then the bitrate as the
last tie-breaker.

**When no copy may replace all the others the answer is to keep them all.**
The classic case: one copy has the original language losslessly and English
only in stereo, the other English in 5.1. Neither covers the other, and a
tool that forced a pick would be deciding something the owner has not. The
verdict is ``KEEP_BOTH``, with every reason.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from mkvkit.config import Config, DedupePolicy
from mkvkit.langcodes import canonical

from .facts import Copy, Track

__all__ = [
    "BLOCKED",
    "KEEP_BOTH",
    "NOT_DUPLICATE",
    "SAFE",
    "VERDICTS",
    "Choice",
    "Coverage",
    "Rules",
    "choose",
    "coverage",
    "rank",
]

SAFE = "SAFE"
KEEP_BOTH = "KEEP_BOTH"
BLOCKED = "BLOCKED"
NOT_DUPLICATE = "NOT_DUPLICATE"
VERDICTS = (SAFE, KEEP_BOTH, BLOCKED, NOT_DUPLICATE)

_ORIGIN_RANK = {"source": 2, "unknown": 1, "re-encode": 0}


@dataclass(frozen=True)
class Rules:
    """The policy the rules read, from ``[policy]`` and ``[policy.dedupe]``."""

    keep_languages: tuple[str, ...] = ()
    droppable_languages: tuple[str, ...] = ()
    dedupe: DedupePolicy = field(default_factory=DedupePolicy)

    @classmethod
    def from_config(cls, config: Config) -> Rules:
        policy = config.policy
        return cls(
            keep_languages=tuple(canonical(c) or c for c in policy.keep_languages),
            droppable_languages=tuple(canonical(c) or c for c in policy.droppable_languages),
            dedupe=policy.dedupe,
        )

    def as_dict(self) -> dict[str, Any]:
        d = self.dedupe
        return {
            "keep_languages": list(self.keep_languages),
            "droppable_languages": list(self.droppable_languages),
            "runtime_tolerance_s": d.runtime_tolerance_s,
            "max_runtime_gap_s": d.max_runtime_gap_s,
            "prefer": list(d.prefer),
            "lossless_codecs": list(d.lossless_codecs),
            "commentary_markers": list(d.commentary_markers),
            "reencode_markers": list(d.reencode_markers),
            "source_markers": list(d.source_markers),
            "keeper_check": d.keeper_check,
            "apply_keeper_check": d.apply_keeper_check,
        }


@dataclass(frozen=True)
class Coverage:
    """Whether ``keeper`` may replace ``loser``, and what would be lost if so."""

    keeper: Copy
    loser: Copy
    #: why it may not; empty when it may
    missing: tuple[str, ...] = ()
    #: what would be lost and is allowed to be
    losses: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing


def _by_language(tracks: Sequence[Track], *, commentary: bool) -> dict[str, list[Track]]:
    out: dict[str, list[Track]] = {}
    for track in tracks:
        if track.commentary == commentary:
            out.setdefault(track.language, []).append(track)
    return out


def _codecs(tracks: Sequence[Track]) -> str:
    return ", ".join(sorted({t.codec or "?" for t in tracks}))


def coverage(keeper: Copy, loser: Copy, rules: Rules) -> Coverage:
    """Every rule, for one pair: may ``keeper`` stand in for ``loser``?"""
    missing: list[str] = []
    losses: list[str] = []
    droppable = set(rules.droppable_languages)

    def refuse(language: str, text: str) -> None:
        if language in droppable:
            losses.append(f"{text} (the policy lets {language} go)")
        else:
            missing.append(text)

    # -- audio, by language, commentary aside
    ours = _by_language(keeper.audio, commentary=False)
    for language, tracks in sorted(_by_language(loser.audio, commentary=False).items()):
        best = max(t.channels for t in tracks)
        theirs = ours.get(language, [])
        if not theirs:
            refuse(language, f"no {language} audio (the other copy has {best} ch)")
            continue
        have = max(t.channels for t in theirs)
        if have < best:
            refuse(language, f"{language} audio at {have} ch where the other copy "
                             f"has {best} ch")
        lossless = [t for t in tracks if t.lossless]
        if lossless and not any(t.lossless for t in theirs):
            refuse(language, f"no lossless {language} audio (the other copy has "
                             f"{_codecs(lossless)})")
        if len(tracks) > len(theirs) and have >= best:
            extra = sorted(tracks, key=lambda t: -t.channels)[len(theirs):]
            losses.append(
                f"{len(extra)} more {language} audio track(s) "
                f"({'; '.join(t.describe() for t in extra)}); {language} is kept at "
                f"{have} ch"
            )

    # -- commentary, by language
    kept_commentary = _by_language(keeper.audio, commentary=True)
    for language, tracks in sorted(_by_language(loser.audio, commentary=True).items()):
        have = len(kept_commentary.get(language, []))
        if have < len(tracks):
            missing.append(
                f"{len(tracks) - have} {language} commentary track(s) the other copy has"
            )

    # -- subtitles
    keep = set(rules.keep_languages)
    theirs_subs: dict[str, list[Track]] = {}
    for track in keeper.subtitles:
        theirs_subs.setdefault(track.language, []).append(track)
    lost_other: list[str] = []
    ours_subs: dict[str, list[Track]] = {}
    for track in loser.subtitles:
        ours_subs.setdefault(track.language, []).append(track)
    for language, tracks in sorted(ours_subs.items()):
        required = (language in keep) if keep else True
        kept = theirs_subs.get(language, [])
        if not required:
            if not kept:
                lost_other.append(language)
            continue
        if not kept:
            refuse(language, f"no {language} subtitles (the other copy has "
                             f"{len(tracks)})")
            continue
        if any(t.forced for t in tracks) and not any(t.forced for t in kept):
            refuse(language, f"no forced {language} subtitles (the other copy has them)")
    if lost_other:
        losses.append(
            f"subtitles in {', '.join(lost_other)}, which the policy does not keep"
        )

    # -- running time
    tolerance = rules.dedupe.runtime_tolerance_s
    if keeper.duration_s is None or loser.duration_s is None:
        missing.append("a running time could not be read, so nothing can be compared")
    elif keeper.duration_s < loser.duration_s - tolerance:
        missing.append(
            f"{loser.duration_s - keeper.duration_s:.0f} s shorter than the other copy "
            f"(the tolerance is {tolerance:.0f} s)"
        )
    return Coverage(keeper=keeper, loser=loser, missing=tuple(missing),
                    losses=tuple(losses))


def _criterion(copy: Copy, name: str) -> float:
    if name == "lossless":
        return 1.0 if copy.lossless else 0.0
    if name == "channels":
        return float(copy.channels)
    if name == "source":
        return float(_ORIGIN_RANK.get(copy.origin, 1))
    if name == "resolution":
        return float(copy.resolution)
    if name == "bitrate":
        return float(copy.bitrate or 0)
    raise ValueError(f"{name}: not a criterion")


def rank(copy: Copy, rules: Rules) -> tuple[float, ...]:
    """The copy's standing under ``policy.dedupe.prefer``; higher is better."""
    return tuple(_criterion(copy, name) for name in rules.dedupe.prefer)


def _why_preferred(keeper: Copy, other: Copy, rules: Rules) -> str:
    for name in rules.dedupe.prefer:
        mine, theirs = _criterion(keeper, name), _criterion(other, name)
        if mine > theirs:
            return name
        if mine < theirs:  # pragma: no cover - the keeper ranks highest
            break
    return "a tie on every criterion"


@dataclass(frozen=True)
class Choice:
    """What the rules decided for one group, before the keeper's payload is read."""

    verdict: str
    keeper: Copy | None = None
    losers: tuple[Copy, ...] = ()
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    coverages: tuple[Coverage, ...] = ()


def _label(copy: Copy) -> str:
    return copy.member.file_name


def choose(copies: Sequence[Copy], rules: Rules) -> Choice:
    """The keeper of a group, or KEEP_BOTH with every reason none may be picked."""
    if len(copies) < 2:
        return Choice(NOT_DUPLICATE, reasons=("fewer than two copies were read",))
    durations = [c.duration_s for c in copies if c.duration_s is not None]
    gap_limit = rules.dedupe.max_runtime_gap_s
    if len(durations) == len(copies) and gap_limit and (
        max(durations) - min(durations) > gap_limit
    ):
        return Choice(KEEP_BOTH, reasons=(
            f"the running times differ by {(max(durations) - min(durations)) / 60:.1f} "
            f"min, more than {gap_limit / 60:.0f} min: different cuts, both kept",
        ))

    table = {
        (i, j): coverage(copies[i], copies[j], rules)
        for i in range(len(copies)) for j in range(len(copies)) if i != j
    }
    eligible = [
        i for i in range(len(copies))
        if all(table[(i, j)].ok for j in range(len(copies)) if j != i)
    ]
    if not eligible:
        reasons = []
        for i in range(len(copies)):
            for j in range(len(copies)):
                if i != j and not table[(i, j)].ok:
                    reasons.append(
                        f"{_label(copies[i])} cannot replace {_label(copies[j])}: "
                        + "; ".join(table[(i, j)].missing)
                    )
                    break
        return Choice(KEEP_BOTH, reasons=tuple(reasons))

    best = max(eligible, key=lambda i: (rank(copies[i], rules), copies[i].size,
                                        copies[i].member.path))
    keeper = copies[best]
    losers = tuple(c for i, c in enumerate(copies) if i != best)
    covers = tuple(table[(best, j)] for j in range(len(copies)) if j != best)
    notes: list[str] = []
    reasons = [
        f"{_label(keeper)} keeps everything the other copies have"
    ]
    for loser in losers:
        reasons.append(
            f"preferred to {_label(loser)} for {_why_preferred(keeper, loser, rules)}"
        )
    tied = [copies[i] for i in eligible if i != best
            and rank(copies[i], rules) == rank(keeper, rules)]
    if tied:
        notes.append(
            "tied on every criterion with " + ", ".join(_label(c) for c in tied)
            + "; the larger file is kept"
        )
    for cover in covers:
        notes += [f"parking {_label(cover.loser)} loses {loss}" for loss in cover.losses]
    return Choice(SAFE, keeper=keeper, losers=losers, reasons=tuple(reasons),
                  notes=tuple(notes), coverages=covers)
