"""mkvkit.chapters.transcripts -- turn a transcript into evidence you can align.

Everything downstream of this module asks the same question: *what is said
between this mark and the next one?* Answering it well turns out to depend
almost entirely on how long the pieces of text are.

**A speech model emits long segments.** A batched transcriber will happily
return twenty or thirty seconds of dialogue as one segment with one start
time. Neither a voice-activity setting nor a chunk length shortens them.
What does work is asking for word
timestamps and re-cutting the words into short cues (:func:`cues_from_words`),
which is what makes everything below possible.

**A cue that straddles a mark belongs to both sides, and to neither.**
Attributing a thirty-second segment entirely to the chapter it starts in puts
the next chapter's opening dialogue into the previous chapter's window, which
is precisely the evidence a name is judged against. So a cue crossing a mark
is *split* at the mark and its words are spread over its own span in
proportion to time (:func:`clip`). Word timings would be better and are used
when they are there; proportional spreading is what is available when they
are not, and it is a good deal better than dropping the cue on one side.

**Rebuilding is idempotent.** :func:`write_windows` never rewrites a file
whose content has not changed, because the modification time is load-bearing
further along: a name list written against a window file is refused if the
window file is newer, and an idle rebuild must not invalidate good names.
The write is also atomic, because the job that produces these runs for hours
and will be interrupted.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import itertools
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_CUE_SECONDS",
    "DEFAULT_GAP_SECONDS",
    "Cue",
    "Word",
    "clip",
    "cues_from_words",
    "parse_srt",
    "total_text",
    "trim_to_marks",
    "write_windows",
]

log = logging.getLogger(__name__)

#: How long a re-cut cue should be. Short enough that a cue rarely crosses a
#: mark, long enough that a sentence usually survives in one piece.
DEFAULT_CUE_SECONDS: Final = 8.0
#: A silence at least this long ends a cue whatever its length.
DEFAULT_GAP_SECONDS: Final = 1.0

_TIMING = re.compile(
    r"(\d{1,2}):([0-5]\d):([0-5]\d)[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
)
#: Markup a subtitle file may carry around its text, in all three dialects.
_MARKUP = re.compile(r"<[^>]+>|\{\\[^}]*\}|\{[^}]*\}")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Cue:
    """A span of time and what is said in it."""

    start_s: float
    end_s: float
    text: str

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def overlaps(self, start_s: float, end_s: float) -> bool:
        return self.end_s > start_s and self.start_s < end_s


@dataclass(frozen=True)
class Word:
    """One word with its own timing, as a transcriber reports it."""

    start_s: float
    end_s: float
    text: str


# ---------------------------------------------------------------------- reading
def parse_srt(text: str) -> list[Cue]:
    """Cues from a subtitle file's text.

    Tolerant on purpose: sequence numbers, markup, positioning braces and the
    two spellings of the decimal separator are all thrown away, and a block
    with no text at all is dropped rather than carried as an empty cue.
    """
    cues: list[Cue] = []
    start = end = 0.0
    lines: list[str] = []
    open_cue = False

    def flush() -> None:
        if open_cue:
            body = " ".join(lines).strip()
            if body:
                cues.append(Cue(start, max(end, start), _WHITESPACE.sub(" ", body)))

    for raw in text.splitlines():
        timing = _TIMING.search(raw)
        if timing:
            flush()
            values = [int(v) for v in timing.groups()]
            start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
            end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
            lines, open_cue = [], True
            continue
        if not open_cue:
            continue
        stripped = raw.strip()
        if not stripped or stripped.isdigit():
            continue
        cleaned = _MARKUP.sub("", stripped).replace("\\N", " ").strip()
        if cleaned:
            lines.append(cleaned)
    flush()
    return cues


def cues_from_words(
    words: Iterable[Word],
    *,
    target_s: float = DEFAULT_CUE_SECONDS,
    gap_s: float = DEFAULT_GAP_SECONDS,
) -> list[Cue]:
    """Re-cut a word stream into short cues.

    A cue ends when it has run for ``target_s``, or when the silence before
    the next word is at least ``gap_s``. This is the fix for a transcriber
    that returns one segment for half a minute of dialogue: the words already
    carry their own times, so nothing is guessed here -- the long segment
    simply stops being the unit anything is attributed to.
    """
    out: list[Cue] = []
    bucket: list[Word] = []

    def close() -> None:
        if bucket:
            text = _WHITESPACE.sub(" ", " ".join(w.text for w in bucket)).strip()
            if text:
                out.append(Cue(bucket[0].start_s, bucket[-1].end_s, text))

    previous_end: float | None = None
    for word in words:
        if bucket and (
            (previous_end is not None and word.start_s - previous_end >= gap_s)
            or word.end_s - bucket[0].start_s > target_s
        ):
            close()
            bucket = []
        bucket.append(word)
        previous_end = word.end_s
    close()
    return out


# --------------------------------------------------------------------- clipping
def clip(cues: Sequence[Cue], start_s: float, end_s: float) -> list[Cue]:
    """The cues inside ``[start_s, end_s)``, each trimmed to its overlap.

    A cue wholly inside the span is kept as it is. A cue crossing either
    boundary keeps the share of its words that falls inside, chosen by where
    the boundary cuts its own span. That is an approximation -- words are not
    evenly spaced in time -- but the alternative is attributing a whole cue to
    a chapter it only half belongs to, and the score a name is judged by is
    computed from exactly this text.

    A cue that overlaps at all always contributes at least one word, so a
    mark landing mid-sentence never silently empties a window.
    """
    out: list[Cue] = []
    for cue in cues:
        if not cue.overlaps(start_s, end_s):
            continue
        if cue.start_s >= start_s and cue.end_s <= end_s:
            out.append(cue)
            continue
        words = cue.text.split()
        if not words:
            continue
        span = max(cue.duration_s, 1e-6)
        low = max(0.0, (start_s - cue.start_s) / span)
        high = min(1.0, (end_s - cue.start_s) / span)
        first = min(round(low * len(words)), len(words) - 1)
        last = max(round(high * len(words)), first + 1)
        text = " ".join(words[first:last])
        if text:
            out.append(
                Cue(max(cue.start_s, start_s), min(cue.end_s, end_s), text)
            )
    return out


def trim_to_marks(cues: Sequence[Cue], marks: Sequence[float]) -> list[Cue]:
    """Split every cue that crosses a mark, so no cue spans two chapters.

    The result is the same text in the same order; only the cue boundaries
    move. Running it twice changes nothing, which matters because the window
    builder may be handed either a raw transcript or one that has already
    been trimmed.
    """
    boundaries = sorted({float(m) for m in marks})
    if not boundaries:
        return list(cues)
    out: list[Cue] = []
    for cue in cues:
        inside = [m for m in boundaries if cue.start_s < m < cue.end_s]
        if not inside:
            out.append(cue)
            continue
        edges = [cue.start_s, *inside, cue.end_s]
        for low, high in itertools.pairwise(edges):
            out.extend(clip([cue], low, high))
    return out


def total_text(cues: Iterable[Cue]) -> str:
    """Every cue's text, in order, as one line."""
    return _WHITESPACE.sub(" ", " ".join(cue.text for cue in cues)).strip()


# ---------------------------------------------------------------------- writing
def write_windows(path: Path | str, text: str) -> bool:
    """Write ``text`` unless the file already says exactly that.

    Returns whether anything was written. The modification time is the reason
    for the comparison: a name list is refused downstream if the window file
    it was written against is newer, so re-running the builder over an
    unchanged transcript must not throw away good names.
    """
    target = Path(path)
    if target.is_file():
        try:
            if target.read_text(encoding="utf-8") == text:
                log.debug("%s is already current", target)
                return False
        except (OSError, UnicodeDecodeError):
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    return True
