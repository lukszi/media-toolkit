"""mkvkit.chapters.verify -- matching marks is not the same as matching names.

The finding this module exists for: a published chapter set whose timestamp
grid matches your cut to within a couple of seconds proves that the MARKS fit.
It says nothing about whether the NAMES were typed against those marks: a
grid can match perfectly while the names describe a scene a mark or more
away.

So names are verified against the content. Twenty seconds of original-language
audio from each mark is transcribed and one frame is taken shortly after it --
both out of a SINGLE seek, because seeking twice per mark is what makes this
too slow to run. Then the content-word hit rate of name *i* is scored against
transcript *i+k* for a range of *k*: a clearly better score at *k* other than
zero is the misalignment signature. The absolute score is weak evidence and is
reported as weak evidence.

Only a set that comes out aligned is eligible to be written. Uncertain is a
verdict, not a rounding error, and a confidently wrong name is worse than no
name at all.

The half of this that is arithmetic now exists elsewhere: matching two grids,
in both rate directions, and the rule that a rate-converted candidate may only
have its names copied onto marks a file already has, live in
:mod:`mkvkit.chapters.grid`. What is left here is the half that needs the
content -- audio, frames, a transcriber -- and it is the half the finding is
about.

**What is mechanical and what is not.** :func:`shift_score` and
:func:`corroborate` are arithmetic and settle the question on their own where
they fire at all. The per-mark call is *not* fully mechanical: the original
work put a person, and then a reading pass, in front of the transcript and the
frame. What :func:`call_marks` implements is the reproducible part of that
rubric -- a name whose words are in its own window is plausible; a name whose
words are in a *different* window and not in its own is describing that other
scene; anything else is unclear -- and a caller who has read the frames can
pass their own calls into :func:`verdict` instead. The frame grab exists for
that reader. Nothing here looks at a picture.

The bar the calls are held to is deliberately lopsided. Refusing to write a
name costs nothing but the name; writing one that describes a different scene
costs a file edit somebody has to find again. So vagueness is never enough to
condemn a name -- disc authors are vague routinely -- and two clear
contradictions, or a shift score that prefers somewhere else, is enough to
hold the whole film back.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from ..config import Config
from ..run import Runner, default_runner
from .grid import MATCH, grid_match
from .xml import ChapterSet, is_generic_name

__all__ = [
    "ALIGNED",
    "DEFAULT_RUBRIC",
    "DEFAULT_SHIFTS",
    "FRAME_OFFSET_S",
    "MIN_SCORED_PAIRS",
    "MIN_SCORED_SHARE",
    "MISALIGNED",
    "PLAUSIBLE",
    "STRUCTURAL_NAMES",
    "UNCERTAIN",
    "UNCLEAR",
    "WINDOW_SECONDS",
    "WRONG",
    "Corroboration",
    "EvidenceProvider",
    "FilmVerdict",
    "MarkCall",
    "MarkEvidence",
    "Rubric",
    "SingleSeekEvidence",
    "Transcribe",
    "better_elsewhere",
    "call_marks",
    "collect_evidence",
    "content_words",
    "corroborate",
    "hit_rate",
    "shift_score",
    "verdict",
]

log = logging.getLogger(__name__)

#: Per-mark calls.
PLAUSIBLE: Final = "PLAUSIBLE"
UNCLEAR: Final = "UNCLEAR"
WRONG: Final = "WRONG"

#: Per-film verdicts. Only the first is eligible to be written.
ALIGNED: Final = "ALIGNED"
MISALIGNED: Final = "MISALIGNED"
UNCERTAIN: Final = "UNCERTAIN"

#: How far the name list is slid against the transcripts, either way.
DEFAULT_SHIFTS: Final[tuple[int, ...]] = (-4, -3, -2, -1, 0, 1, 2, 3, 4)
#: Fewest name-and-window pairs an offset may be scored on at all.
MIN_SCORED_PAIRS: Final = 3
#: And at least this share of the names that can be scored anywhere.
MIN_SCORED_SHARE: Final = 0.5

#: Seconds of audio taken from each mark.
WINDOW_SECONDS: Final = 20.0
#: How far after the mark the frame is taken: far enough to be past a fade.
FRAME_OFFSET_S: Final = 3.0

#: Names that describe a film's structure rather than a scene. They are
#: plausible where such a name belongs and say nothing anywhere else, so they
#: are never scored and never condemned.
STRUCTURAL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "main titles", "opening titles", "opening credits", "title sequence",
        "titles", "logos", "prologue", "vorspann",
        "end credits", "closing credits", "credits", "epilogue", "abspann",
    }
)

#: Words that say nothing about which scene a name belongs to. A name made
#: only of these cannot be checked, which is a result rather than a failure.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    """the a an and or of to in on at for with from by is are was were be been
    being this that these those it its his her their your our my he she they we
    you as not no so if then than part one two three four five six seven eight
    nine ten end ends ending opening open main title titles credits credit
    chapter scene finale prologue epilogue intro introduction start starts
    begins beginning final last first new old up down out off into over under
    again back away mr mrs dr get gets got go goes going come comes coming take
    takes taking make makes making have has had do does did what who whom whose
    where when why how all any some more most""".split()
)

_WORD = re.compile(r"[^0-9A-Za-zÀ-ɏ']+")
_SUFFIXES: Final[tuple[str, ...]] = ("ing", "ed", "es", "s")


# ------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class MarkEvidence:
    """What was found at one mark."""

    index: int
    time_s: float
    name: str | None = None
    transcript: str = ""
    frame: Path | None = None
    note: str = ""

    @property
    def has_speech(self) -> bool:
        return bool(self.transcript.strip())


@dataclass(frozen=True)
class MarkCall:
    """One mark's verdict and the sentence behind it."""

    index: int
    call: str
    reason: str
    best_elsewhere: int | None = None

    @property
    def checkable(self) -> bool:
        return self.call != UNCLEAR

    def __str__(self) -> str:
        return f"{self.index:>3}  {self.call:<9} {self.reason}"


@dataclass(frozen=True)
class ShiftScores:
    """The keyword hit rate of the name list at every offset tried."""

    scores: Mapping[int, float | None] = field(default_factory=dict)
    at_zero: float | None = None
    best_shift: int = 0
    best_score: float | None = None
    scored_names: int = 0

    @property
    def margin(self) -> float:
        """How much better the best offset is than no offset at all."""
        if self.best_score is None or self.at_zero is None:
            return 0.0
        return self.best_score - self.at_zero

    def misaligned(self, *, threshold: float) -> bool:
        """Whether the list scores clearly better somewhere other than here."""
        return self.best_shift != 0 and self.margin >= threshold

    @property
    def strength(self) -> str:
        """How much the score at no offset is worth on its own.

        Weak is the common case and is not an accusation: plenty of real disc
        names share no word with the dialogue at their own mark.
        """
        if self.at_zero is None:
            return "no content words"
        if self.at_zero >= 0.30:
            return "strong"
        if self.at_zero >= 0.18:
            return "fair"
        return "weak"

    def __str__(self) -> str:
        row = " ".join(
            f"{k:+d}:{v:.3f}" for k, v in sorted(self.scores.items()) if v is not None
        )
        return f"hit rate by offset: {row or '(nothing scoreable)'}"


@dataclass(frozen=True)
class Rubric:
    """The bar a film has to clear. Every number is a policy, not a constant."""

    #: Share of marks that have to be checkable at all.
    min_checkable: float = 0.60
    #: Share of the checkable ones that have to be plausible.
    min_plausible: float = 0.80
    #: This many clear contradictions and the film is held back.
    max_wrong: int = 1
    #: How much better a non-zero offset has to score before it counts.
    shift_margin: float = 0.06
    #: Share of a name's content words that has to turn up in another window
    #: before that window is called a better home for it.
    elsewhere_fraction: float = 0.50


DEFAULT_RUBRIC: Final = Rubric()


@dataclass(frozen=True)
class FilmVerdict:
    """One film's answer, with everything it was computed from."""

    verdict: str
    reason: str
    calls: tuple[MarkCall, ...] = ()
    shifts: ShiftScores | None = None

    @property
    def writable(self) -> bool:
        """Only an aligned set may have its names written into a file."""
        return self.verdict == ALIGNED

    @property
    def counts(self) -> dict[str, int]:
        out = {PLAUSIBLE: 0, UNCLEAR: 0, WRONG: 0}
        for call in self.calls:
            out[call.call] += 1
        return out

    def __str__(self) -> str:
        counts = self.counts
        return (
            f"{self.verdict}: {self.reason} "
            f"({counts[PLAUSIBLE]} plausible, {counts[UNCLEAR]} unclear, "
            f"{counts[WRONG]} wrong, of {len(self.calls)} marks)"
        )


# -------------------------------------------------------------------- providers
@runtime_checkable
class EvidenceProvider(Protocol):
    """Produces the evidence for one mark.

    One call per mark, returning both pieces, because they come out of one
    seek. A provider that returns an empty transcript is saying "nothing was
    said here", which is a real answer; a provider that cannot answer at all
    raises.
    """

    def collect(
        self, media: Path, index: int, at_s: float
    ) -> tuple[str, Path | None]:
        """The transcript from this mark, and a frame shortly after it."""


class SingleSeekEvidence:
    """The real provider: one decoder invocation per mark, two outputs.

    Twenty seconds of audio and one frame come out of the same seek. Doing
    them separately doubles the number of seeks, and on a collection with
    thousands of marks on rotating storage the seeks are the entire cost.

    The audio is the track the work was *made* in. Transcribing a dub and
    comparing it against names written for the original is a guaranteed miss
    that looks exactly like misalignment.

    The transcriber is injected and is the only part that needs a model. A
    provider with no transcriber still produces the frames, which is a
    perfectly reasonable thing to want.
    """

    def __init__(
        self,
        *,
        transcribe: Transcribe | None = None,
        audio_stream: int = 0,
        frames_dir: Path | None = None,
        seconds: float = WINDOW_SECONDS,
        frame_offset_s: float = FRAME_OFFSET_S,
        language: str | None = None,
        runner: Runner | None = None,
        config: Config | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.transcribe = transcribe
        self.audio_stream = audio_stream
        self.frames_dir = frames_dir
        self.seconds = seconds
        self.frame_offset_s = frame_offset_s
        self.language = language
        self.run = runner if runner is not None else default_runner(config)
        self.work_dir = work_dir or Path(".")

    def command(self, media: Path, index: int, at_s: float) -> list[str]:
        """The one invocation. Separate so it can be asserted in a test."""
        audio = self.work_dir / f"mark{index:04d}.wav"
        args = [
            "-nostdin", "-v", "error",
            "-ss", f"{at_s:.3f}", "-i", str(media),
            "-map", f"0:a:{self.audio_stream}", "-t", f"{self.seconds:.3f}",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(audio),
        ]
        frame = self._frame_path(index)
        if frame is not None:
            args += [
                "-map", "0:v:0", "-ss", f"{self.frame_offset_s:.3f}",
                "-frames:v", "1", "-vf", "scale=480:-1", "-q:v", "4",
                "-y", str(frame),
            ]
        return args

    def _frame_path(self, index: int) -> Path | None:
        if self.frames_dir is None:
            return None
        return self.frames_dir / f"mark{index:04d}.jpg"

    def collect(self, media: Path, index: int, at_s: float) -> tuple[str, Path | None]:
        audio = self.work_dir / f"mark{index:04d}.wav"
        frame = self._frame_path(index)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if frame is not None:
            frame.parent.mkdir(parents=True, exist_ok=True)
        self.run("ffmpeg", self.command(media, index, at_s))
        try:
            if self.transcribe is None:
                return "", frame
            return self.transcribe(audio, language=self.language), frame
        finally:
            audio.unlink(missing_ok=True)


class Transcribe(Protocol):
    """Turns a short audio file into text. The only thing that needs a model."""

    def __call__(self, audio: Path, *, language: str | None = None) -> str: ...


def collect_evidence(
    media: Path | str,
    marks: ChapterSet | Sequence[float],
    *,
    provider: EvidenceProvider,
    names: Sequence[str | None] | None = None,
) -> list[MarkEvidence]:
    """Evidence for every mark, in order.

    A mark whose collection fails is recorded with the reason rather than
    dropped: a pass over a collection has to be able to say which marks it
    could not read, and a missing mark silently shifts every index after it,
    which is the exact failure this module exists to detect.
    """
    target = Path(media)
    starts = (
        list(marks.starts_s) if isinstance(marks, ChapterSet)
        else [float(m) for m in marks]
    )
    if names is None:
        names = (
            list(marks.names) if isinstance(marks, ChapterSet) else [None] * len(starts)
        )
    out: list[MarkEvidence] = []
    for index, at_s in enumerate(starts, start=1):
        name = names[index - 1] if index - 1 < len(names) else None
        try:
            transcript, frame = provider.collect(target, index, at_s)
            out.append(MarkEvidence(index, at_s, name, transcript.strip(), frame))
        except Exception as exc:
            log.warning("mark %d at %.3f s: %s", index, at_s, exc)
            out.append(MarkEvidence(index, at_s, name, note=f"not collected: {exc}"))
    return out


# ------------------------------------------------------------------- the scores
def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_words(name: str | None) -> list[str]:
    """The words in a name that could identify a scene.

    Short words and the words every name contains are dropped, because a name
    matching a window on "the" is not a match.
    """
    if not name or is_generic_name(name):
        return []
    if name.strip().casefold() in STRUCTURAL_NAMES:
        return []
    return [
        word
        for word in _WORD.split(name.casefold())
        if len(word) >= 3 and word not in _STOPWORDS
    ]


def hit_rate(name: str | None, text: str) -> tuple[int, int] | None:
    """How many of a name's content words appear in a text, and how many there were.

    ``None`` where the name has no content words at all: that is "cannot be
    scored", which is not the same as "scored zero" and must not be averaged
    in as though it were.
    """
    words = content_words(name)
    if not words:
        return None
    haystack = " " + _WORD.sub(" ", (text or "").casefold()).strip() + " "
    stems = {_stem(w) for w in haystack.split()}
    hits = sum(1 for word in words if _stem(word) in stems or _stem(word) in haystack)
    return hits, len(words)


def shift_score(
    names: Sequence[str | None],
    transcripts: Sequence[str],
    *,
    shifts: Sequence[int] = DEFAULT_SHIFTS,
    min_pairs: int = MIN_SCORED_PAIRS,
    min_share: float = MIN_SCORED_SHARE,
) -> ShiftScores:
    """Score the name list against the transcripts at each offset.

    At offset *k*, name *i* is scored against the window of mark *i+k*. If the
    list was typed against a different set of marks -- an insert, a menu, a
    different edition -- the offset that recovers the original pairing scores
    clearly better than zero, and that difference is the signature. It is one
    of only two pieces of evidence here that settle the question on their own.
    """
    scores: dict[int, float | None] = {}
    scored_names = sum(1 for name in names if content_words(name))
    # An offset near the end of the range has only a few names left inside the
    # list, and two names agreeing perfectly is not a better answer than forty
    # names agreeing well. So an offset that cannot be scored on enough of the
    # list is not scored at all, rather than scored on what is left.
    least = max(min_pairs, round(min_share * scored_names))
    for shift in shifts:
        hits = total = pairs = 0
        for index, name in enumerate(names):
            other = index + shift
            if not 0 <= other < len(transcripts):
                continue
            rate = hit_rate(name, transcripts[other])
            if rate is None:
                continue
            hits += rate[0]
            total += rate[1]
            pairs += 1
        scores[shift] = round(hits / total, 3) if total and pairs >= least else None
    scoreable = {k: v for k, v in scores.items() if v is not None}
    if not scoreable:
        return ShiftScores(scores=scores, scored_names=scored_names)
    best = max(scoreable, key=lambda k: (scoreable[k], -abs(k)))
    return ShiftScores(
        scores=scores,
        at_zero=scores.get(0),
        best_shift=best,
        best_score=scoreable[best],
        scored_names=scored_names,
    )


# -------------------------------------------------------------------- the calls
def call_marks(
    evidence: Sequence[MarkEvidence], *, rubric: Rubric = DEFAULT_RUBRIC
) -> list[MarkCall]:
    """The reproducible half of the rubric, one call per mark.

    Three outcomes, and the asymmetry between them is the whole design.
    *Plausible*: something the name says is said in its own window. *Wrong*:
    nothing the name says is in its own window and most of it is in another
    one -- the name describes a scene that starts somewhere else. *Unclear*:
    everything else, including every silent window and every name too generic
    to check. Unclear is the default, not the exception.

    A vague, poetic or end-of-window name is never called wrong here. Disc
    authors write those on purpose, and calling them wrong would hold back
    films whose names are perfectly usable.
    """
    transcripts = [e.transcript for e in evidence]
    calls: list[MarkCall] = []
    for position, item in enumerate(evidence):
        name = (item.name or "").strip()
        if item.note:
            calls.append(MarkCall(item.index, UNCLEAR, item.note))
            continue
        if not name:
            calls.append(MarkCall(item.index, UNCLEAR, "no name is proposed here"))
            continue
        if name.casefold() in STRUCTURAL_NAMES:
            structural_ok = position == 0 or position == len(evidence) - 1
            calls.append(
                MarkCall(
                    item.index,
                    PLAUSIBLE if structural_ok else UNCLEAR,
                    "a structural name where one belongs"
                    if structural_ok
                    else "a structural name away from the ends; nothing to check it on",
                )
            )
            continue
        own = hit_rate(name, item.transcript)
        if own is None:
            calls.append(
                MarkCall(item.index, UNCLEAR, "the name carries no word to look for")
            )
            continue
        if not item.has_speech:
            calls.append(
                MarkCall(item.index, UNCLEAR, "nothing is said in this window")
            )
            continue
        if own[0] > 0:
            calls.append(
                MarkCall(
                    item.index, PLAUSIBLE,
                    f"{own[0]} of {own[1]} word(s) from the name are said here",
                )
            )
            continue
        elsewhere = better_elsewhere(
            name, transcripts, position, fraction=rubric.elsewhere_fraction
        )
        if elsewhere is not None:
            calls.append(
                MarkCall(
                    item.index, WRONG,
                    f"none of the name is said here; {elsewhere[1]:.0%} of it is said "
                    f"at mark {elsewhere[0] + 1}, which has a name of its own",
                    best_elsewhere=elsewhere[0] + 1,
                )
            )
            continue
        calls.append(
            MarkCall(
                item.index, UNCLEAR,
                "nothing in the window ties to the name or contradicts it",
            )
        )
    return calls


def better_elsewhere(
    name: str,
    transcripts: Sequence[str],
    position: int,
    *,
    fraction: float = 0.50,
    short_name_words: int = 3,
) -> tuple[int, float] | None:
    """The window a name fits better than its own, if one clearly does.

    The bar rises for a short name, and that is not a detail. A two-word name
    matching one of its two words somewhere else is half of nothing: it shares
    an ordinary word with an ordinary sentence. A name with fewer than
    ``short_name_words`` content words therefore has to turn up *whole*
    somewhere else before that somewhere else is called its real home, which
    is the difference between catching a misplaced name and inventing one.
    """
    words = content_words(name)
    if not words:
        return None
    needed = 1.0 if len(words) < short_name_words else fraction
    best: tuple[int, float] | None = None
    for other, text in enumerate(transcripts):
        if other == position or not text.strip():
            continue
        rate = hit_rate(name, text)
        if rate is None or rate[1] == 0:
            continue
        share = rate[0] / rate[1]
        if share >= needed and (best is None or share > best[1]):
            best = (other, share)
    return best


def verdict(
    evidence: Sequence[MarkEvidence],
    shifts: ShiftScores | None = None,
    *,
    rubric: Rubric = DEFAULT_RUBRIC,
    calls: Sequence[MarkCall] | None = None,
) -> FilmVerdict:
    """The film's answer, recomputed from the per-mark calls.

    The calls are never taken as the answer, even when a reader supplies
    them: the thresholds are applied here, in one place, so that a pass whose
    rubric was sharpened halfway through can be re-run over the calls it
    already has without re-reading anything.
    """
    marks = list(calls) if calls is not None else call_marks(evidence, rubric=rubric)
    if not marks:
        return FilmVerdict(UNCERTAIN, "there are no marks to check", (), shifts)
    counts = {PLAUSIBLE: 0, UNCLEAR: 0, WRONG: 0}
    for call in marks:
        counts[call.call] += 1
    checkable = counts[PLAUSIBLE] + counts[WRONG]
    checkable_share = checkable / len(marks)

    if shifts is not None and shifts.misaligned(threshold=rubric.shift_margin):
        return FilmVerdict(
            MISALIGNED,
            f"the name list scores better against the windows {shifts.best_shift:+d} "
            f"mark(s) away ({shifts.best_score} against {shifts.at_zero})",
            tuple(marks), shifts,
        )
    if counts[WRONG] > rubric.max_wrong:
        return FilmVerdict(
            MISALIGNED,
            f"{counts[WRONG]} names describe a scene that starts at another mark",
            tuple(marks), shifts,
        )
    if counts[WRONG]:
        return FilmVerdict(
            UNCERTAIN,
            "one name describes another mark's scene; one is not a pattern and is "
            "not a clean list either",
            tuple(marks), shifts,
        )
    if checkable_share < rubric.min_checkable:
        return FilmVerdict(
            UNCERTAIN,
            f"only {checkable_share:.0%} of the marks could be checked at all, "
            f"under the {rubric.min_checkable:.0%} this needs",
            tuple(marks), shifts,
        )
    plausible_share = counts[PLAUSIBLE] / checkable if checkable else 0.0
    if plausible_share < rubric.min_plausible:
        return FilmVerdict(
            UNCERTAIN,
            f"{plausible_share:.0%} of the checkable marks are plausible, under "
            f"{rubric.min_plausible:.0%}",
            tuple(marks), shifts,
        )
    return FilmVerdict(
        ALIGNED,
        f"no contradiction, {checkable_share:.0%} of the marks checkable and "
        f"{plausible_share:.0%} of those plausible",
        tuple(marks), shifts,
    )


# ------------------------------------------------------------- a second opinion
@dataclass(frozen=True)
class Corroboration:
    """What other published lists for the same cut say about this one."""

    agreeing: tuple[str, ...] = ()
    same_name_order: int = 0
    marks: int = 0
    borrowed_from: tuple[str, ...] = ()

    @property
    def corroborated(self) -> bool:
        """Two contributors, the same marks, the same names in the same order."""
        return bool(self.agreeing) and self.same_name_order >= max(1, self.marks - 1)

    def __str__(self) -> str:
        if self.borrowed_from:
            return (
                "this name list is also published for "
                f"{', '.join(self.borrowed_from)}, whose marks are different: it is "
                "the archive's list, not this film's"
            )
        if not self.agreeing:
            return "no other published list matches these marks"
        return (
            f"{len(self.agreeing)} other list(s) match these marks; the closest "
            f"agrees on {self.same_name_order} of {self.marks} names"
        )


def _normalise(name: str | None) -> str:
    return " ".join(_WORD.sub(" ", (name or "").casefold()).split())


def corroborate(
    candidate: ChapterSet,
    others: Iterable[tuple[str, ChapterSet]],
    *,
    marks: ChapterSet | None = None,
) -> Corroboration:
    """Compare a candidate against every other published list you have.

    Cheap, offline, and one of the two things here that settles the question
    on its own: a second contributor who typed the same names against the same
    marks, years apart and without coordination, is evidence no transcript can
    match.

    It also catches the other direction. A list that is *identical* to one
    published for a different cut -- shared typo and all -- is the archive's
    list rather than this film's, and its marks agreeing means nothing.
    """
    reference = marks if marks is not None else candidate
    ours = [_normalise(name) for name in candidate.names]
    agreeing: list[str] = []
    best_same = 0
    borrowed: list[str] = []
    for label, other in others:
        theirs = [_normalise(name) for name in other.names]
        if len(other) == len(reference) and grid_match(
            reference, other
        ).verdict == MATCH:
            agreeing.append(label)
            same = sum(1 for a, b in zip(ours, theirs, strict=False) if a and a == b)
            best_same = max(best_same, same)
        elif ours and theirs == ours:
            borrowed.append(label)
    return Corroboration(
        agreeing=tuple(agreeing),
        same_name_order=best_same,
        marks=len(candidate),
        borrowed_from=tuple(borrowed),
    )
