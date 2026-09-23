"""mkvkit.langid.priors -- context that is evidence, kept apart from the audio.

A track that the audio cannot settle rarely sits in a vacuum. The file has
other audio tracks; the episode has twenty siblings in the same season, tagged
by the same hand; the filename may say outright which languages the release
carries. Those facts are evidence, and a pipeline that ignores them spends
hours of reading on questions already answered.

They are also the most dangerous evidence in the pipeline, because they are
correlated with exactly the mistakes they would confirm. So the priors here
obey three rules, and every one of them is a rule about *not* concluding:

1. **A prior never settles a track on its own** when the track has speech in
   it. It can confirm what the audio already says, or lower the audio bar a
   little; it cannot speak for the audio.
2. **A prior is blocked outright** for the material where the context is
   systematically wrong: extras and featurettes, specials, commentary,
   descriptive-audio, karaoke and score tracks, very short clips, and disc
   folders whose track order is not the order the catalogue reports.
3. **Two priors that disagree cancel** rather than vote. A conflict is a
   signal that the context is not what it looks like.

Two priors are implemented, and they are deliberately separate because only
one of them has anything to do with a media server:

``release_token_prior`` reads language claims out of a filename. Pure string
work, no I/O, useful on its own, and the easiest thing here to test.

``sibling_prior`` uses the tracks around this one -- the same ordinal position
across the other episodes of a season, which is the same track in the same
mux. Tracks are laid out by a person to complement each other, so their
languages are not independent draws.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from ..langcodes import canonical

__all__ = [
    "BLOCKING_DIRECTORIES",
    "RELEASE_TOKENS",
    "Prior",
    "SiblingVote",
    "TrackKind",
    "blocking_reasons",
    "combine",
    "release_token_prior",
    "sibling_prior",
    "track_kind",
    "trimmed_mean",
]

#: Filename tokens that state a language outright. The dual-language marker is
#: the interesting one: in release naming ``DL`` beside a language name means
#: that language *and* the original, which is nearly always English -- so the
#: token predicts two tracks, not one, and is only useful together with how
#: many audio tracks the file actually has.
RELEASE_TOKENS: Mapping[str, frozenset[str]] = {
    "english": frozenset({"eng"}),
    "german": frozenset({"deu"}),
    "deutsch": frozenset({"deu"}),
    "french": frozenset({"fra"}),
    "francais": frozenset({"fra"}),
    "italian": frozenset({"ita"}),
    "spanish": frozenset({"spa"}),
    "castellano": frozenset({"spa"}),
    "latino": frozenset({"spa"}),
    "portuguese": frozenset({"por"}),
    "russian": frozenset({"rus"}),
    "japanese": frozenset({"jpn"}),
    "korean": frozenset({"kor"}),
    "mandarin": frozenset({"zho"}),
    "cantonese": frozenset({"yue"}),
    "hindi": frozenset({"hin"}),
    "polish": frozenset({"pol"}),
    "czech": frozenset({"ces"}),
    "dutch": frozenset({"nld"}),
    "swedish": frozenset({"swe"}),
    "danish": frozenset({"dan"}),
    "norwegian": frozenset({"nor"}),
    "finnish": frozenset({"fin"}),
    "turkish": frozenset({"tur"}),
    "persian": frozenset({"fas"}),
    "farsi": frozenset({"fas"}),
    "arabic": frozenset({"ara"}),
    "hebrew": frozenset({"heb"}),
    "eng-ita": frozenset({"eng", "ita"}),
    "ita-eng": frozenset({"eng", "ita"}),
    "ger-eng": frozenset({"deu", "eng"}),
    "eng-ger": frozenset({"deu", "eng"}),
    "multi": frozenset(),
}

#: Directory names whose contents are not the main feature, and whose language
#: is routinely different from everything around them.
BLOCKING_DIRECTORIES = re.compile(
    r"\A(extras?|featurettes?|behind[ ._-]the[ ._-]scenes|deleted[ ._-]scenes|"
    r"interviews?|trailers?|teasers?|samples?|shorts|scenes|bonus.*|specials?)\Z",
    re.IGNORECASE,
)

#: Track titles that announce what a track is for. A commentary track is in the
#: language of the people talking over the film, which is frequently not the
#: language of the film; a descriptive-audio track is narration; a score track
#: has no language at all.
_TITLE_KINDS: Mapping[str, tuple[str, ...]] = {
    "commentary": ("commentary", "kommentar", "director"),
    "descriptive": (
        "audio description", "audiodeskription", "descriptive", "described",
        "visually impaired", "narration",
    ),
    "karaoke": ("karaoke",),
    "score": ("score", "isolated", "music only", "instrumental"),
}

#: A clip this short is a trailer, a sting or a menu loop.
SHORT_CLIP_S = 180.0

#: Sibling strength, as (minimum votes, minimum purity) pairs. Either pair
#: qualifies: ten votes at ninety per cent, or five that are unanimous.
STRONG_SIBLING: tuple[tuple[int, float], ...] = ((10, 0.90), (5, 1.0))
MEDIUM_SIBLING: tuple[tuple[int, float], ...] = ((3, 1.0),)


@dataclass(frozen=True)
class Prior:
    """What the context says, how strongly, and where it came from."""

    language: str | None = None
    strength: str | None = None  # "strong" | "medium" | None
    sources: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    conflict: str | None = None

    def __bool__(self) -> bool:
        return bool(self.language and self.strength and not self.blocked)


@dataclass(frozen=True)
class SiblingVote:
    """One neighbouring track's believed language, and how it is believed."""

    language: str
    source: str = "tag"  # "tag" | "audio"
    scope: str = "season"


@dataclass(frozen=True)
class TrackKind:
    """What a track is for, as far as its title admits."""

    kind: str | None = None
    title: str | None = None


def track_kind(title: str | None) -> TrackKind:
    """Classify a track title, case-insensitively, by keyword.

        >>> track_kind("Director's Commentary").kind
        'commentary'
        >>> track_kind("Surround 5.1").kind is None
        True
    """
    text = (title or "").strip().lower()
    for kind, needles in _TITLE_KINDS.items():
        if any(needle in text for needle in needles):
            return TrackKind(kind=kind, title=title)
    return TrackKind(kind=None, title=title)


def _parts(path: str) -> tuple[str, list[str]]:
    """Split a path written for either platform into (name, parent names)."""
    pure: PurePath = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return pure.name, list(pure.parts[:-1])


def blocking_reasons(
    path: str,
    *,
    title: str | None = None,
    runtime_s: float | None = None,
    is_special: bool = False,
    is_disc_folder: bool = False,
) -> tuple[str, ...]:
    """Why the context must not be trusted for this track, if it must not.

    Being on this list does not stop the audio from settling anything; it
    stops the *priors* from being used, which is the difference between "the
    evidence was thin" and "the evidence was thin and we guessed".
    """
    reasons: list[str] = []
    name, parents = _parts(path)
    if any(BLOCKING_DIRECTORIES.match(part) for part in parents):
        reasons.append("extra")
    if is_special:
        reasons.append("special")
    kind = track_kind(title).kind
    if kind:
        reasons.append(kind)
    if runtime_s is not None and 0 < runtime_s < SHORT_CLIP_S:
        reasons.append("clip")
    if re.search(r"\b(trailer|teaser|karaoke)\b", name, re.IGNORECASE):
        reasons.append("clip")
    if is_disc_folder:
        reasons.append("disc-folder")
    return tuple(dict.fromkeys(reasons))


def _tokens(path: str) -> set[str]:
    name, parents = _parts(path)
    context = f"{name} {parents[-1] if parents else ''}".lower()
    return {token for token in re.split(r"[^a-z0-9-]+", context) if token}


def release_token_prior(
    filename: str, *, audio_track_count: int = 1, known: Iterable[str] = ()
) -> Prior:
    """Languages a release name claims, reduced to one if that is possible.

    A claim of two languages tells you nothing about *this* track until you
    know what the other tracks are. When the file carries exactly as many
    audio tracks as the name claims languages, and every other track's
    language is already known and accounted for, exactly one claim is left --
    and that one is this track's.

        >>> release_token_prior("Harbour.Lights.S01E02.German.DL.mkv",
        ...                     audio_track_count=2, known=["deu"]).language
        'eng'
        >>> release_token_prior("Harbour.Lights.S01E02.mkv").language is None
        True
    """
    tokens = _tokens(filename)
    claimed: set[str] = set()
    hits: list[str] = []
    for token, languages in RELEASE_TOKENS.items():
        if token in tokens and languages:
            claimed |= set(languages)
            hits.append(token)
    if "dl" in tokens and claimed:
        # dual language: the named language plus the original, in practice English
        claimed |= {"eng"}
        hits.append("dl")
    if not claimed:
        return Prior(sources=())

    settled = {code for code in (canonical(k) for k in known) if code}
    remaining = claimed - settled
    sources = (f"release:{'+'.join(sorted(hits))}",)
    if len(claimed) == audio_track_count and len(remaining) == 1 and \
            len(settled) == audio_track_count - 1:
        return Prior(language=next(iter(remaining)), strength="medium", sources=sources)
    if audio_track_count == 1 and len(claimed) == 1:
        return Prior(language=next(iter(claimed)), strength="medium", sources=sources)
    return Prior(sources=sources)


def sibling_prior(votes: Sequence[SiblingVote]) -> Prior:
    """What the same track position says across the neighbouring items.

    Leave-one-out by construction: the caller passes the siblings, never this
    track, because a prior that can see its own subject is not a prior.

    Strength comes from both count and purity. Three unanimous neighbours are
    medium; ten at ninety per cent, or five unanimous, are strong. Anything
    less returns the majority language with no strength, which records what
    the context said without letting it lower any bar.
    """
    languages = [v.language for v in votes if v.language]
    if len(languages) < 3:
        return Prior()
    counted = Counter(languages)
    language, hits = counted.most_common(1)[0]
    purity = hits / len(languages)
    scope = votes[0].scope if votes else "season"
    source = f"sibling:{scope}:{hits}/{len(languages)}"
    strength: str | None = None
    if any(hits >= n and purity >= p for n, p in STRONG_SIBLING):
        strength = "strong"
    elif any(hits >= n and purity >= p for n, p in MEDIUM_SIBLING):
        strength = "medium"
    return Prior(language=language, strength=strength, sources=(source,))


def combine(*priors: Prior, blocked: Sequence[str] = ()) -> Prior:
    """Merge the priors into one, and let disagreement cancel them.

    Agreement raises confidence to the strongest of the agreeing sources;
    disagreement produces a prior with no language at all and the conflict
    recorded, because two context signals pointing different ways is evidence
    that the context is not what it appears to be.
    """
    sources = tuple(s for prior in priors for s in prior.sources)
    if blocked:
        return Prior(sources=sources, blocked=tuple(blocked))
    usable = [p for p in priors if p.language and p.strength]
    if not usable:
        return Prior(sources=sources)
    languages = {p.language for p in usable}
    if len(languages) > 1:
        return Prior(
            sources=sources,
            conflict="+".join(sorted(str(lang) for lang in languages)),
        )
    strength = "strong" if any(p.strength == "strong" for p in usable) else "medium"
    return Prior(language=usable[0].language, strength=strength, sources=sources)


def trimmed_mean(values: Sequence[float], frac: float) -> float:
    """Mean after dropping the lowest ``floor(n * frac)`` values.

    One-sided on purpose: this is used where a small number of windows are
    expected to be genuinely unlike the rest -- a scene in another language --
    and the question is whether the remainder agrees, not whether the extremes
    are symmetrical.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    drop = int(len(ordered) * frac)
    kept = ordered[drop:] or ordered
    return sum(kept) / len(kept)
