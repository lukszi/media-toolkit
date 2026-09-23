"""mkvkit.chapters.windows -- one window of text per mark, and when to refuse.

A window is everything said between one mark and the next. It is the only
material a name is written from and the only material a name is checked
against, so two decisions about it decide the quality of everything
downstream.

**Size the window by how long the chapter is, not by a flat number of
characters.** A fixed cap is fine for a five-minute chapter and wrong for a
very long one. Worse, the obvious way of enforcing a cap -- keep the head and
the tail, elide the middle -- removes exactly the part of a long chapter that
a name should describe. The name then comes from the chapter's opening and
closing moments while the elided middle held the sequence the chapter is
about, and that is the window's fault rather than the namer's.

So the budget here is proportional to the chapter's own duration inside a
floor and a ceiling (:func:`budget_for`), and when text has to be dropped it
is dropped **evenly across the whole span** (:func:`build_windows`): the
chapter is cut into slots, each slot contributes its share, and ``[...]``
marks each elision. Every part of a long chapter is represented.

**Refuse thin material rather than describing it.** A transcript that is a
stub, or that stops two thirds of the way through the film, produces windows
that look perfectly well-formed and are empty where it matters -- and an
empty window reads as "no dialogue here", which is a rule that fires and
writes a name. :func:`refusals` is the guard: too few cues in total, or
coverage that stops short of the runtime, and the film is skipped with the
reason recorded. A skipped film costs nothing; a film named from half a
transcript costs a file edit that has to be found later.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from .transcripts import Cue, clip, total_text
from .xml import ChapterSet

__all__ = [
    "CHARS_PER_MINUTE",
    "ELISION",
    "MAX_SLOTS",
    "MAX_WINDOW_CHARS",
    "MIN_COVERAGE",
    "MIN_CUES",
    "MIN_SLOTS",
    "MIN_WINDOW_CHARS",
    "NO_DIALOGUE",
    "SLOT_SECONDS",
    "Window",
    "WindowSet",
    "budget_for",
    "build_windows",
    "refusals",
]

log = logging.getLogger(__name__)

# The character budgets and the two refusal thresholds below are *fitted*
# defaults, not derived ones; see `docs/methods/chapter-names.md`. A
# collection of half-hour episodes or of heavily dialogued material wants
# different ones; every one of them is an argument to the function that
# uses it, so re-fitting is a call site rather than an edit here.

#: Characters of window per minute of chapter. Dialogue density varies far
#: more than this between films; the number only has to be large enough that
#: an ordinary chapter is never cut at all.
CHARS_PER_MINUTE: Final = 500
#: No window is ever smaller than this, however short the chapter.
MIN_WINDOW_CHARS: Final = 1_500
#: No window is ever larger than this, however long the chapter.
MAX_WINDOW_CHARS: Final = 6_000
#: What marks a gap left by even sampling. A gap, not a cut to the end.
ELISION: Final = " [...] "
#: What a window with nothing said in it carries instead of text.
NO_DIALOGUE: Final = "(no dialogue in this chapter)"

#: Fewer cues than this over a whole film and the transcript is a stub or an
#: extraction still in flight.
MIN_CUES: Final = 60
#: The last cue has to reach at least this far into the runtime.
MIN_COVERAGE: Final = 0.55
#: Slots an over-budget window is sampled across: one per two minutes, inside
#: these bounds, so a long chapter is represented everywhere along its span.
MIN_SLOTS: Final = 4
MAX_SLOTS: Final = 24
SLOT_SECONDS: Final = 120.0


@dataclass(frozen=True)
class Window:
    """The text of one chapter, ready to be read or scored."""

    index: int
    start_s: float
    end_s: float
    text: str
    cues: int = 0
    sampled: bool = False

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def has_dialogue(self) -> bool:
        """Whether anything is said here at all.

        This is a mechanical fact about the window, and the rule it drives --
        what to call a chapter nobody speaks in -- is mechanical for the same
        reason. Leaving it to judgement is how a silent chapter acquires an
        invented name.
        """
        return self.cues > 0 and self.text != NO_DIALOGUE

    def __str__(self) -> str:
        flags = []
        if self.sampled:
            flags.append("evenly sampled, [...] marks elisions")
        if not self.has_dialogue:
            flags.append("NO DIALOGUE")
        suffix = f", {'; '.join(flags)}" if flags else ""
        return (
            f"[{self.index}] {self.duration_s / 60:.0f} min, "
            f"{self.cues} cue(s){suffix}\n{self.text}"
        )


@dataclass(frozen=True)
class WindowSet:
    """Every window of one film, plus what a reader needs to know about them."""

    windows: tuple[Window, ...] = ()
    runtime_s: float = 0.0
    names_language: str = "eng"
    transcript_language: str | None = None
    source: str = ""

    def __len__(self) -> int:
        return len(self.windows)

    def __iter__(self) -> Iterator[Window]:
        return iter(self.windows)

    def __getitem__(self, index: int) -> Window:
        return self.windows[index]

    @property
    def silent(self) -> tuple[int, ...]:
        return tuple(w.index for w in self.windows if not w.has_dialogue)

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(w.text if w.has_dialogue else "" for w in self.windows)

    def render(self) -> str:
        """The whole set as text, for a person or a program to read.

        **No absolute timestamp appears anywhere in it.** A name is returned
        per window *number*, the marks are carried through from the file
        untouched, and a name carrying a timecode can then only have come
        from one that was shown -- which makes that rule checkable instead of
        hopeful. Durations are shown because they say how much of the film a
        window covers; positions are not, because they are not needed.
        """
        head = [
            f"RUNTIME: {self.runtime_s / 60:.0f} min   WINDOWS: {len(self.windows)}",
            f"NAMES MUST BE WRITTEN IN: {self.names_language}"
            + (
                f"   (the text below is {self.transcript_language})"
                if self.transcript_language
                and self.transcript_language != self.names_language
                else ""
            ),
            f"TRANSCRIPT SOURCE: {self.source or 'unknown'}",
            "",
            "One name per window number. Never output a timecode.",
            "",
        ]
        return "\n".join([*head, *(f"{window}\n" for window in self.windows)])


# ----------------------------------------------------------------- the refusals
def refusals(
    cues: Sequence[Cue],
    *,
    runtime_s: float,
    min_cues: int = MIN_CUES,
    min_coverage: float = MIN_COVERAGE,
) -> list[str]:
    """Why this transcript must not be used. Empty means it may be.

    Both checks catch the same class of failure -- material that looks fine
    and is not there -- and both are cheap enough to run before anything else.
    """
    problems: list[str] = []
    if len(cues) < min_cues:
        problems.append(
            f"only {len(cues)} cue(s) in the whole film; under {min_cues} means a "
            "stub or an extraction that has not finished"
        )
    if runtime_s > 0 and cues:
        last = max(cue.end_s for cue in cues)
        if last < min_coverage * runtime_s:
            problems.append(
                f"the transcript stops at {last / 60:.0f} min of "
                f"{runtime_s / 60:.0f} min; under {min_coverage:.0%} of the runtime "
                "is not a transcript of this film"
            )
    if runtime_s <= 0:
        problems.append("no runtime was given, so coverage cannot be checked")
    return problems


# ------------------------------------------------------------------ the builder
def budget_for(
    duration_s: float,
    *,
    per_minute: int = CHARS_PER_MINUTE,
    minimum: int = MIN_WINDOW_CHARS,
    maximum: int = MAX_WINDOW_CHARS,
) -> int:
    """How many characters this chapter's window may hold.

    Proportional to the chapter's own length, between a floor and a ceiling.
    The floor keeps a two-minute chapter from being cut at all; the ceiling
    keeps a half-hour chapter from producing more text than anything reads.
    """
    scaled = round(per_minute * max(0.0, duration_s) / 60.0)
    return max(minimum, min(maximum, scaled))


def build_windows(
    cues: Sequence[Cue],
    marks: ChapterSet | Sequence[float],
    *,
    runtime_s: float,
    names_language: str = "eng",
    transcript_language: str | None = None,
    source: str = "",
    per_minute: int = CHARS_PER_MINUTE,
) -> WindowSet:
    """One window per mark, evenly sampled where the text exceeds its budget.

    The last window runs from the last mark to the runtime. Marks are used
    exactly as the file carries them and are never adjusted, rounded or
    re-derived: this function only decides which text belongs to which of
    them.
    """
    starts = (
        list(marks.starts_s) if isinstance(marks, ChapterSet)
        else [float(m) for m in marks]
    )
    if not starts:
        return WindowSet(
            (), runtime_s=runtime_s, names_language=names_language,
            transcript_language=transcript_language, source=source,
        )
    bounds = [*starts, max(runtime_s, starts[-1] + 1.0)]
    windows: list[Window] = []
    for index, (start, end) in enumerate(itertools.pairwise(bounds), start=1):
        selected = clip(cues, start, end)
        budget = budget_for(end - start, per_minute=per_minute)
        text, sampled = _sample(selected, start, end, budget)
        windows.append(
            Window(
                index=index, start_s=start, end_s=end,
                text=text or NO_DIALOGUE, cues=len(selected), sampled=sampled,
            )
        )
    return WindowSet(
        tuple(windows), runtime_s=runtime_s, names_language=names_language,
        transcript_language=transcript_language, source=source,
    )


def _sample(
    cues: Sequence[Cue], start_s: float, end_s: float, budget: int
) -> tuple[str, bool]:
    """The chapter's text, cut down to ``budget`` characters if it has to be.

    Under budget, everything is kept. Over budget, the span is divided into
    slots and each slot gives up the same share, so the result covers the
    whole chapter rather than its ends.
    """
    if not cues:
        return "", False
    whole = total_text(cues)
    if len(whole) <= budget:
        return whole, False

    span = max(1.0, end_s - start_s)
    slots = max(MIN_SLOTS, min(MAX_SLOTS, round(span / SLOT_SECONDS)))
    per_slot = max(200, budget // slots)
    parts: list[str] = []
    elided = False
    for slot in range(slots):
        low = start_s + span * slot / slots
        high = start_s + span * (slot + 1) / slots
        chunk = total_text(c for c in cues if low <= c.start_s < high)
        if not chunk:
            continue
        if len(chunk) > per_slot:
            chunk = chunk[:per_slot].rsplit(" ", 1)[0]
            elided = True
        parts.append(chunk)
    if not parts:
        return whole[:budget].rsplit(" ", 1)[0], True
    return ELISION.join(parts), elided
