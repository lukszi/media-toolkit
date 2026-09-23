"""mkvkit.chapters.sources -- where a chapter list comes from, and which job it is.

Two things live here. A **protocol** for anything that can offer chapter
lists for a title, with one worked adapter for the shape those sources
actually have -- a search that returns several candidates per title and a
document per candidate. And the **classification** that decides what may be
done with a candidate once it has been found, which is the part that keeps a
collection safe.

A candidate is only ever one of three jobs, and they carry very different
risk:

``A`` -- *times and names*
    The file has no marks of its own. Both the marks and the names come from
    the candidate. There is nothing in the file to check the candidate
    against, so the only evidence is the runtime, and a runtime agreeing to
    half a minute is not evidence: unrelated features routinely run that
    close. This job is therefore refused for anything rate-converted, and
    wants a person to look at the list before it is written.

``B`` -- *names onto marks you already have*
    The file's own marks stay exactly where they are and only the names are
    taken. This is the cheapest and safest job in the whole area, because the
    worst case is a wrong name on a mark that has not moved -- and it is only
    offered where the two grids agree to within the tolerance, which is what
    proves the candidate describes this cut.

``C`` -- *times only*
    The candidate has marks but no names worth the word. Unnamed marks are of
    some use for scrubbing and of no use for anything else, so these are
    parked rather than written: the classification names the bucket and stops.

The counting behind "no names worth the word" is the one that has to be
careful. A list of bare timecodes, or entries reading ``Chapter One``,
``Chapter Two`` and so on in some language, is not a name
list -- and a naive "is the string non-empty" count accepts all of them.
:func:`mkvkit.chapters.xml.is_generic_name` is what does the counting here,
and lists like that do get written into files by a pass that counts the
other way.

Nothing in this module goes near a network of its own accord. The adapter
takes the function that fetches, so a test passes a dictionary; it is
disabled unless a caller enables it explicitly; and it caches, rate-limits
and identifies itself when it is enabled.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, runtime_checkable
from urllib.parse import quote

from .grid import GridMatch, grid_match, runtime_match
from .xml import Chapter, ChapterError, ChapterSet, parse_timestamp

__all__ = [
    "BUCKET_A",
    "BUCKET_B",
    "BUCKET_C",
    "DEFAULT_MIN_INTERVAL_S",
    "DEFAULT_USER_AGENT",
    "MIN_NAMED_FRACTION",
    "NOT_USABLE",
    "RUNTIME_TOLERANCE_S",
    "Candidate",
    "ChapterDatabase",
    "ChapterSource",
    "Classification",
    "Fetch",
    "SourceDisabled",
    "classify",
    "named_fraction",
    "parse_duration",
    "pick",
    "title_variants",
]

log = logging.getLogger(__name__)

#: The file has no marks: take the candidate's times *and* names.
BUCKET_A: Final = "times-and-names"
#: The file has marks the candidate agrees with: take the names only.
BUCKET_B: Final = "names-only"
#: The candidate has marks but nothing that counts as a name.
BUCKET_C: Final = "times-only"
#: Nothing about this candidate may be written anywhere.
NOT_USABLE: Final = "not-usable"

#: Below this share of real names, a candidate is a times-only list.
MIN_NAMED_FRACTION: Final = 0.5
#: How close two runtimes have to be before a candidate is even considered.
RUNTIME_TOLERANCE_S: Final = 30.0

#: One request every this many seconds, at most. These archives are somebody's
#: hobby server and the whole point of the cache below is to ask once.
DEFAULT_MIN_INTERVAL_S: Final = 0.6
#: Says what the program is and that it is a survey tool, not a crawler.
DEFAULT_USER_AGENT: Final = "mkvkit/chapters (library survey tool)"

#: Takes a URL and headers, returns the body as text.
Fetch = Callable[[str, Mapping[str, str]], str]


class SourceDisabled(RuntimeError):
    """A source that has to be enabled explicitly was used without enabling it."""


# ------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class Candidate:
    """One offered chapter list, before anything has been fetched or checked.

    ``runtime_s`` and ``marks`` are what the listing claims. Both are hints:
    the listing is free text somebody typed, and the only numbers that decide
    anything are the ones read out of the document itself.
    """

    id: str
    title: str
    year: int | None = None
    runtime_s: float | None = None
    marks: int | None = None
    confirmations: int = 0
    source: str = ""

    def __str__(self) -> str:
        year = f" ({self.year})" if self.year else ""
        runtime = f", {self.runtime_s / 60:.0f} min" if self.runtime_s else ""
        return f"{self.title}{year} [{self.source}:{self.id}{runtime}]"


@dataclass(frozen=True)
class Classification:
    """Which of the three jobs a candidate is, and why."""

    bucket: str
    reason: str
    match: GridMatch | None = None
    named: int = 0
    marks: int = 0

    @property
    def usable(self) -> bool:
        """Whether anything may be written from this candidate at all."""
        return self.bucket in {BUCKET_A, BUCKET_B}

    @property
    def needs_a_person(self) -> bool:
        """Bucket A has no evidence beyond a runtime. Somebody should look."""
        return self.bucket == BUCKET_A

    def __str__(self) -> str:
        return f"{self.bucket}: {self.reason}"


@runtime_checkable
class ChapterSource(Protocol):
    """Anything that can offer chapter lists for a title.

    Two calls, deliberately: the listing is cheap and the documents are not,
    so a caller filters on the listing and fetches only what survives.
    """

    name: str

    def search(self, title: str, year: int | None = None) -> Sequence[Candidate]:
        """Candidates for this title, best first if the source has an order."""

    def get(self, candidate_id: str) -> ChapterSet:
        """The marks and names of one candidate."""


# ------------------------------------------------------------------- the counts
def named_fraction(chapters: ChapterSet) -> float:
    """The share of marks carrying something that is actually a name.

    A label is not a name. This is the guard that keeps a list of eighteen
    timecodes, or eighteen spellings of "chapter *n*", from being written into
    a file as though somebody had described the film.
    """
    if not len(chapters):
        return 0.0
    return chapters.named_count / len(chapters)


def classify(
    marks: ChapterSet | None,
    candidate: ChapterSet,
    *,
    runtime_s: float | None = None,
    candidate_runtime_s: float | None = None,
    min_named: float = MIN_NAMED_FRACTION,
    runtime_tolerance_s: float = RUNTIME_TOLERANCE_S,
) -> Classification:
    """Decide which of the three jobs this candidate is, for this file.

    ``marks`` is what the file already has; pass ``None`` or an empty set for
    a file with none. Nothing here writes, fetches or opens anything.
    """
    named = candidate.named_count
    n_candidate = len(candidate)
    n_file = len(marks) if marks is not None else 0

    if n_candidate == 0:
        return Classification(NOT_USABLE, "the candidate has no marks at all")

    share = named_fraction(candidate)
    has_names = share >= min_named

    if n_file:
        assert marks is not None
        match = grid_match(marks, candidate)
        if not match.usable_for_names:
            return Classification(
                NOT_USABLE,
                f"the grids do not agree ({match}); names are never best-effort "
                "aligned onto different marks",
                match=match, named=named, marks=n_file,
            )
        if not has_names:
            return Classification(
                BUCKET_C,
                f"the grids agree but only {named} of {n_candidate} entries is a "
                "name rather than a label; the file already has these marks",
                match=match, named=named, marks=n_file,
            )
        return Classification(
            BUCKET_B,
            f"the grids agree ({match}); the file keeps its own marks and takes "
            f"{named} name(s)",
            match=match, named=named, marks=n_file,
        )

    # No marks in the file. There is nothing to compare, so the runtime is the
    # only filter there is -- and it is a filter, not evidence.
    if runtime_s is not None and candidate_runtime_s is not None:
        fits, scale_name = runtime_match(
            runtime_s, candidate_runtime_s, tolerance_s=runtime_tolerance_s
        )
        if not fits:
            return Classification(
                NOT_USABLE,
                f"the runtimes do not fit at any scale ({runtime_s:.0f} s against "
                f"{candidate_runtime_s:.0f} s)",
                named=named, marks=0,
            )
        if scale_name != "as written":
            return Classification(
                NOT_USABLE,
                "the runtime only fits after a rate conversion, and this file has "
                "no marks to check that against; a converted list from another cut "
                "fits nothing",
                named=named, marks=0,
            )
    if not has_names:
        return Classification(
            BUCKET_C,
            f"only {named} of {n_candidate} entries is a name rather than a "
            "label, so this is a list of times",
            named=named, marks=0,
        )
    return Classification(
        BUCKET_A,
        f"the file has no marks; the candidate offers {n_candidate} with "
        f"{named} name(s), on a runtime that fits",
        named=named, marks=0,
    )


def pick(
    marks: ChapterSet | None,
    candidates: Iterable[tuple[Candidate, ChapterSet]],
    *,
    runtime_s: float | None = None,
) -> tuple[Candidate, ChapterSet, Classification] | None:
    """The best usable candidate, or ``None``.

    Best means: the safest job first (names onto your own marks before marks
    and names together), then the most names, then the most confirmations. The
    closest runtime is deliberately *not* the first key -- an archive usually
    holds several lists per title and the one describing your cut is the one
    whose grid fits, whatever its stated runtime says.
    """
    scored: list[tuple[tuple[int, int, int], Candidate, ChapterSet, Classification]] = []
    for candidate, chapters in candidates:
        found = classify(
            marks, chapters,
            runtime_s=runtime_s, candidate_runtime_s=candidate.runtime_s,
        )
        if not found.usable:
            continue
        rank = 1 if found.bucket == BUCKET_B else 0
        scored.append(((rank, found.named, candidate.confirmations),
                       candidate, chapters, found))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0], reverse=True)
    _, candidate, chapters, found = scored[0]
    return candidate, chapters, found


# ------------------------------------------------------------- title variations
_ROMAN = {"2": "II", "3": "III", "4": "IV", "5": "V",
          "II": "2", "III": "3", "IV": "4", "V": "5"}
_ARTICLE = re.compile(r"^(?:the|a|an|der|die|das|le|la|les|el|il)\s+", re.IGNORECASE)
_SEQUEL = re.compile(r"\b(2|3|4|5|II|III|IV|V)\b")


def title_variants(title: str, *, limit: int = 4) -> list[str]:
    """Other spellings of a title worth trying against a free-text index.

    These archives are keyed on whatever a contributor typed, years ago, in
    whichever language the disc in front of them used. A miss is far more
    often a spelling than an absence, so a handful of obvious re-spellings is
    the cheapest coverage there is: the ampersand both ways, either side of a
    colon, the leading article dropped, and a sequel number in the other
    numbering.
    """
    base = unicodedata.normalize("NFC", title).strip()
    out: list[str] = []
    if "&" in base:
        out.append(base.replace("&", "and"))
    if re.search(r"\band\b", base, re.IGNORECASE):
        out.append(re.sub(r"\band\b", "&", base, flags=re.IGNORECASE))
    if ":" in base:
        head, _, tail = base.partition(":")
        out += [head.strip(), tail.strip()]
    if _ARTICLE.match(base):
        out.append(_ARTICLE.sub("", base))
    found = _SEQUEL.search(base)
    if found:
        out.append(base[: found.start()] + _ROMAN[found.group(1)] + base[found.end():])

    seen = {base.casefold()}
    unique: list[str] = []
    for value in out:
        value = value.strip(" -")
        key = value.casefold()
        if len(value) >= 3 and key not in seen:
            seen.add(key)
            unique.append(value)
    return unique[:limit]


# ------------------------------------------------------------------- an adapter
_ROW = re.compile(
    r"<td[^>]*>(?P<kind>[^<]*)</td>\s*"
    r"<td[^>]*><a href=\"[^\"]*/(?P<id>\d+)\"[^>]*>(?P<title>.*?)</a></td>\s*"
    r"<td[^>]*>(?P<duration>[^<]*)</td>",
    re.DOTALL,
)
_CONFIRMATIONS = re.compile(r"class=\"[^\"]*confirm[^\"]*\"[^>]*>(\d+)<")
_CHAPTER = re.compile(r"<chapter\s+time=\"([^\"]+)\"(?:\s+name=\"([^\"]*)\")?\s*/>")
_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class ChapterDatabase:
    """One worked adapter for a scraped chapter archive.

    The shape is the one these archives have: a listing page per free-text
    title with several candidates on it, and one XML document per candidate.
    It is written as an adapter rather than as *the* source so that another
    index, a local directory of documents, or a recorded fixture can take its
    place without anything above it changing.

    Four properties are not optional, and each is here because scraping
    somebody's archive is a favour being asked:

    * **off unless enabled.** Constructing this does nothing. Every call that
      would leave the machine raises :class:`SourceDisabled` until
      ``enabled`` is set, so the source cannot be reached by accident through
      a default.
    * **cached on disk.** A re-run costs no request at all, which also makes
      a survey reproducible and a test possible.
    * **rate-limited.** One request per ``min_interval_s``, measured across
      the whole adapter rather than per call site.
    * **identified.** The user agent says what the program is.

    ``fetch`` is injected. The tests pass a dictionary; nothing in this
    package's test suite opens a socket.
    """

    base_url: str
    fetch: Fetch
    name: str = "chapter-database"
    enabled: bool = False
    cache_dir: Path | None = None
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    user_agent: str = DEFAULT_USER_AGENT
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _last_request: float = field(default=0.0, repr=False)
    requests: int = field(default=0, repr=False)
    cache_hits: int = field(default=0, repr=False)

    # ------------------------------------------------------------------ fetching
    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SourceDisabled(
                f"{self.name} is a scraped archive and ships disabled. Enable it "
                "explicitly (--enable-chapterdb on the command line, or "
                "enabled=True here) once you are content to send it requests."
            )

    def _cached(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / key

    def _body(self, url: str, key: str) -> str:
        cached = self._cached(key)
        if cached is not None and cached.is_file():
            self.cache_hits += 1
            return cached.read_text(encoding="utf-8")
        self._require_enabled()
        wait = self.min_interval_s - (self.clock() - self._last_request)
        if self.requests and wait > 0:
            self.sleep(wait)
        log.info("%s: fetching %s", self.name, url)
        body = self.fetch(url, {"User-Agent": self.user_agent})
        self._last_request = self.clock()
        self.requests += 1
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(body, encoding="utf-8", newline="\n")
        return body

    # -------------------------------------------------------------- the protocol
    def search(self, title: str, year: int | None = None) -> list[Candidate]:
        """Candidates for a title, most confirmed first.

        ``year`` is used to order rather than to filter: these listings carry
        a year inconsistently, and dropping every row without one throws away
        most of the archive.
        """
        body = self._body(
            f"{self.base_url}/browse?title={_quote(title)}",
            _cache_key("s", title) + ".html",
        )
        found = self.parse_listing(body)
        if year is not None:
            found.sort(key=lambda c: (c.year != year, -c.confirmations))
        return found

    def get(self, candidate_id: str) -> ChapterSet:
        """One candidate's document."""
        body = self._body(
            f"{self.base_url}/browse/{candidate_id}.xml", f"c_{candidate_id}.xml"
        )
        return self.parse_document(body)

    # ------------------------------------------------------------------ parsing
    def parse_listing(self, body: str) -> list[Candidate]:
        """The rows of a listing page. Separate so it can be tested on its own."""
        confirmations = [int(value) for value in _CONFIRMATIONS.findall(body)]
        out: list[Candidate] = []
        for index, row in enumerate(_ROW.finditer(body)):
            title = html.unescape(row.group("title")).strip()
            year = _YEAR.search(title)
            out.append(
                Candidate(
                    id=row.group("id"),
                    title=title,
                    year=int(year.group(0)) if year else None,
                    runtime_s=parse_duration(row.group("duration")),
                    confirmations=(
                        confirmations[index] if index < len(confirmations) else 0
                    ),
                    source=self.name,
                )
            )
        return out

    def parse_document(self, body: str) -> ChapterSet:
        """The marks and names of one candidate document."""
        marks = _CHAPTER.findall(body)
        if not marks:
            raise ChapterError("the document carries no chapter elements")
        title = _TITLE.search(body)
        chapters = []
        for timestamp, name in marks:
            start = parse_timestamp(timestamp)
            if start is None:
                raise ChapterError(f"unreadable timestamp {timestamp!r}")
            text = html.unescape(name or "").strip()
            chapters.append(Chapter(start_ns=start, name=text or None))
        return ChapterSet(
            tuple(chapters),
            source=html.unescape(title.group(1)).strip() if title else self.name,
        )


def parse_duration(text: str | None) -> float | None:
    """A listing's duration column, in seconds.

    These columns are typed by hand and come in two shapes -- hours, minutes
    and a fraction, or hours, minutes and seconds. A zero duration means the
    contributor left it blank and is returned as "no answer" rather than as
    a runtime of nothing, because a file whose runtime is compared against
    zero fails a check it should never have been given.
    """
    if not text:
        return None
    value = text.strip()
    found = re.match(r"^(\d+):(\d{1,2})[.](\d{1,2})$", value)
    if found:
        seconds = int(found.group(1)) * 3600 + int(found.group(2)) * 60 + int(
            found.group(3)
        )
        return float(seconds) or None
    found = re.match(r"^(\d+):(\d{1,2}):(\d{1,2})", value)
    if found:
        seconds = (
            int(found.group(1)) * 3600 + int(found.group(2)) * 60 + int(found.group(3))
        )
        return float(seconds) or None
    return None


def _cache_key(prefix: str, value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", unicodedata.normalize("NFC", value))
    return f"{prefix}_{slug[:80]}"


def _quote(value: str) -> str:
    return quote(value)
