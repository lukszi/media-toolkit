"""jfkit.naming -- predict how a media server will read a filename, before renaming.

Renaming an episode is cheap. Renaming it into a shape the server parses
differently is not: the item gets a new identity, its watch state does not
follow, and a file that parses as an episode *range* makes the metadata
provider concatenate the titles and plots of every episode in that range, on
every refresh, for ever. This module answers the question first, on a string,
with no server and no file.

**What it is.** A port of the episode-path expressions Jellyfin 12.1 uses --
the ``SxxEyy`` expression, the absolute-number expression, and the ten
multi-episode expressions it runs afterwards to attach an end number. Given a
path it returns what the server will make of it.

**What it is not.** It is somebody else's parser, reimplemented, and that is a
standing hazard: the day the upstream expressions change, this becomes
confidently wrong rather than obviously broken. Three things keep that
honest -- the version it was checked against is recorded in
:data:`PARSED_AGAINST`, every behaviour is pinned by a fixture table of
invented names, and anything the ported expressions do not model is reported
as :attr:`Rule.RECONSTRUCTED` or :attr:`Rule.BY_DATE` rather than quietly
returned as "no match". A silent "nothing here" on a name the server *does*
parse is the failure mode that costs a day.

**Four behaviours worth knowing before you rename anything.**

*An ``SxxEyy`` token claims the name and suppresses the absolute expression.*
That is why adding one is the durable cure for a name that parses as a range:
the absolute expression is guarded by a negative lookahead for exactly that
token. The guard, like every expression here, only looks at the **last path
segment** -- a season folder called ``S01`` does not protect the files inside
it.

*A bare number after a space is an absolute episode number,* and
``<name> 1-05 <junk>.avi`` is read as episode 1 with an end number of 5.
A four-digit year in a title is read as episode 1985.

*The end-number expressions run either way.* Whatever claimed the name first,
the ten multi-episode expressions are applied afterwards and can still attach
an end number.

*A date in the name is a different kind of episode entirely.* Those get their
numbers re-derived from the path on every refresh, so an index number written
through the API does not survive. This module flags them; the ported
expressions cannot express them.

    >>> clean = "/srv/media/series/Northwind - S01E03 - The Quiet Harbour.mkv"
    >>> parse(clean).season, parse(clean).episode, parse(clean).end
    (1, 3, None)
    >>> ranged = parse("/srv/media/series/Harbour Lights 1-05 Pilot.avi")
    >>> ranged.episode, ranged.end, ranged.would_get_end
    (1, 5, True)
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath, PurePosixPath, PureWindowsPath

__all__ = [
    "PARSED_AGAINST",
    "VIDEO_SUFFIXES",
    "Parse",
    "Rule",
    "clean_name",
    "is_clean",
    "parse",
    "parse_many",
]

log = logging.getLogger(__name__)

#: The release these expressions were read from and checked against. This is
#: the single most important line in the module: without it, a user cannot
#: tell whether the answer applies to the server they are running.
PARSED_AGAINST = "Jellyfin 12.1, September 2026"

#: Suffixes the directory walk considers. The expressions themselves never
#: look at the suffix -- this only decides which files a directory scan asks
#: about, and a server resolves a wider set than this.
VIDEO_SUFFIXES = frozenset(
    {".avi", ".mkv", ".mp4", ".m4v", ".mpg", ".mpeg", ".ts", ".wmv", ".mov", ".m2ts"}
)


class Rule(Enum):
    """Which expression claimed the name."""

    #: An ``SxxEyy`` marker. The clean case, and the one to rename towards.
    SEASON_EPISODE = "season-episode"
    #: A bare or hyphenated number, read as an absolute episode number.
    ABSOLUTE = "absolute"
    #: An ``NxNN`` form. The upstream primary expression for this is not part
    #: of the ported set; the numbers here come from the multi-episode
    #: expression that matched, and are reported as reconstructed.
    RECONSTRUCTED = "reconstructed"
    #: A date in the name. Handled by a different upstream path entirely: the
    #: numbers are re-derived on every refresh and cannot be written durably.
    BY_DATE = "by-date"
    #: Nothing matched. Given the note above about what is not modelled, this
    #: means "no expression here claims it", not "the server will ignore it".
    NONE = "none"


@dataclass(frozen=True)
class Parse:
    """What the server will make of one path."""

    path: str
    season: int | None = None
    episode: int | None = None
    end: int | None = None
    rule: Rule = Rule.NONE
    multi: bool = False
    notes: tuple[str, ...] = ()

    @property
    def would_get_end(self) -> bool:
        """True when this name produces an episode *range*.

        Worth its own name because it is the expensive outcome: a range is not
        writable through the API and is refilled from the path on any refresh
        that runs a provider, so the only cure is the rename this module
        exists to plan.
        """
        return self.end is not None

    @property
    def numbered(self) -> bool:
        return self.episode is not None

    def matches(self, season: int | None, episode: int | None) -> bool:
        return self.season == season and self.episode == episode and self.end is None

    def describe(self) -> str:
        parts = [
            f"S={self.season if self.season is not None else '-'}",
            f"E={self.episode if self.episode is not None else '-'}",
            f"end={self.end if self.end is not None else '-'}",
            f"[{self.rule.value}{'+multi' if self.multi else ''}]",
        ]
        return " ".join(parts)


# --------------------------------------------------------------------- patterns
# The expressions are written against a path, not a bare name, and both
# separators are accepted. Every one of them is scoped to the LAST segment:
# the leading `.*[\\/]` consumes the directories, and every character class
# after it excludes both separators. That is upstream behaviour and not a
# simplification -- a parent directory contributes nothing.

_SEASON_EPISODE = re.compile(
    r".*[\\/](?P<seriesname>((?![Ss]([0-9]+)[][ ._-]*[Ee]([0-9]+))[^\\/])*)?"
    r"[Ss](?P<seasonnumber>[0-9]+)[][ ._-]*[Ee](?P<epnumber>[0-9]+)([^\\/]*)$"
)

# The absolute expression, with its two guards intact:
#   (?![Ee]pisode)   a name that starts with the word "Episode" is excluded
#   (?![^\\/]*Sxx..Eyy)  an SxxEyy token anywhere in the SAME segment wins
# and its tail `[^\\/x]*$`, which is why a lower-case "x" after the number --
# in a codec token, for instance -- stops this expression from matching at all.
_ABSOLUTE = re.compile(
    r".*[\\/](?![Ee]pisode)(?![^\\/]*[Ss][0-9]+[][ ._-]*[Ee][0-9]+)"
    r"(?P<seriesname>[\w\s]+?)\s(?P<epnumber>[0-9]{1,4})"
    r"(-(?P<endingepnumber>[0-9]{2,4}))*[^\\/x]*$"
)

# The ten multi-episode expressions, run AFTER whatever claimed the name and
# able to attach an end number either way. They are written here in Python
# syntax on purpose: translating .NET group syntax at import time with a blind
# `(?<` -> `(?P<` substitution corrupts any lookbehind that is added later.
_MULTI_SOURCES: tuple[str, ...] = (
    r".*[\\/][sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
    r"((-| - )[0-9]{1,4}[eExX](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/][sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
    r"((-| - )[0-9]{1,4}[xX][eE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/][sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
    r"((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/][sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
    r"(-[xX]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\/])*)?"
    r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
    r"((-| - )[0-9]{1,4}[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\/])*)?"
    r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
    r"((-| - )[0-9]{1,4}[xX][eE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\/])*)?"
    r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
    r"((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\/])*)?"
    r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
    r"(-[xX]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>[^\\/]*)[sS](?P<seasonnumber>[0-9]{1,4})"
    r"[xX.]?[eE](?P<epnumber>[0-9]{1,3})"
    r"((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
    r".*[\\/](?P<seriesname>[^\\/]*)[sS](?P<seasonnumber>[0-9]{1,4})"
    r"[xX.]?[eE](?P<epnumber>[0-9]{1,3})"
    r"(-[xX]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+[^\\/]*$",
)
_MULTI = tuple(re.compile(source) for source in _MULTI_SOURCES)

# The bare ``NxNN`` form. Upstream has a primary expression for it; the ported
# set does not, and a name this module cannot claim is exactly the answer that
# misleads -- "no expression here matches" reads like "the server will ignore
# it", and it will not. The numbers come from the same shape the multi-episode
# expressions use for it, and every parse from here is flagged as reconstructed.
_CROSS = re.compile(
    r".*[\\/](?![^\\/]*[Ss][0-9]+[][ ._-]*[Ee][0-9]+)[^\\/]*?"
    r"(?<![0-9])(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})(?![0-9])"
    r"[^\\/]*$"
)

#: A date anywhere in the segment, in any of the separators that occur in the
#: wild. Not one of the ported expressions -- it exists so that a name the
#: ported expressions would misreport as an absolute number is flagged instead.
_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-._ ]\d{2}[-._ ]\d{2}(?!\d)")

_SEASON_FOLDER = re.compile(
    r"\A(?:season|staffel|saison|series|temporada|sezon)[ ._-]*(?P<number>\d{1,4})\Z"
    r"|\A[sS](?P<short>\d{1,4})\Z",
    re.IGNORECASE,
)


def _segments(path: str) -> tuple[str, list[str]]:
    """Split a path written for either platform into (last segment, parents)."""
    pure: PurePath = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    parts = [p for p in pure.parts if p not in ("/", "\\")]
    if not parts:
        return path, []
    return parts[-1], parts[:-1]


def _anchored(name: str) -> str:
    """Give the expressions the separator they require, without inventing one.

    Every upstream expression starts by consuming directories, so a bare
    basename matches nothing at all. The original tool worked around this by
    prefixing the working directory and rewriting every separator to a
    backslash, which quietly makes the answer depend on where the tool was
    run. Prefixing a single separator is the same thing without either
    surprise.
    """
    return "/" + name


def parse(path: str | PurePath) -> Parse:
    """Predict season, episode and end number for one path.

    Only the final path segment is examined by the expressions, exactly as
    upstream does it; the parent directories are used for one thing only,
    which is to notice a season folder and say so in the notes.
    """
    text = str(path)
    name, parents = _segments(text)
    anchored = _anchored(name)
    notes: list[str] = []

    season: int | None = None
    episode: int | None = None
    end: int | None = None
    rule = Rule.NONE

    match = _SEASON_EPISODE.match(anchored)
    if match:
        season = int(match.group("seasonnumber"))
        episode = int(match.group("epnumber"))
        rule = Rule.SEASON_EPISODE
    elif _DATE.search(name):
        rule = Rule.BY_DATE
        notes.append(
            "a date in the name makes this a by-date episode: index numbers are "
            "re-derived from the path on every refresh and cannot be written durably"
        )
    else:
        match = _ABSOLUTE.match(anchored)
        if match:
            episode = int(match.group("epnumber"))
            raw_end = match.group("endingepnumber")
            end = int(raw_end) if raw_end else None
            rule = Rule.ABSOLUTE
            notes.append(
                "read as an absolute episode number; adding an SxxEyy token to the "
                "name suppresses this expression"
            )

    # The multi-episode expressions run afterwards whatever happened above, and
    # can attach an end number to a name that looked clean.
    multi = False
    for pattern in _MULTI:
        found = pattern.match(anchored)
        if not found or not found.group("endingepnumber"):
            continue
        end = int(found.group("endingepnumber"))
        multi = True
        if rule is Rule.NONE:
            # An NxNN name. The upstream primary expression for this shape is
            # not in the ported set, so the numbers come from the multi
            # expression's own groups and the parse is flagged as such.
            season = int(found.group("seasonnumber"))
            episode = int(found.group("epnumber"))
            rule = Rule.RECONSTRUCTED
            notes.append(
                "an NxNN form: the numbers are reconstructed from a multi-episode "
                "expression, because the primary expression for this shape is not "
                "part of the ported set"
            )
        break

    if rule is Rule.NONE:
        crossed = _CROSS.match(anchored)
        if crossed:
            season = int(crossed.group("seasonnumber"))
            episode = int(crossed.group("epnumber"))
            rule = Rule.RECONSTRUCTED
            notes.append(
                "an NxNN form: the numbers are reconstructed, because the primary "
                "expression for this shape is not part of the ported set"
            )

    if end is not None:
        notes.append(
            "this name produces an episode range; the end number is refilled from "
            "the path on any refresh that runs a provider, so only a rename clears it"
        )

    if season is None:
        folder_season = _season_from_folders(parents)
        if folder_season is not None:
            notes.append(
                f"the parent folder names season {folder_season}; the expressions "
                "never read it, but the server does"
            )

    return Parse(
        path=text, season=season, episode=episode, end=end, rule=rule,
        multi=multi, notes=tuple(notes),
    )


def _season_from_folders(parents: Sequence[str]) -> int | None:
    """The season a parent folder announces, if one of them does.

    A season folder needs a keyword upstream: a folder called ``Season 01`` or
    ``S01`` names a season, and one called ``01 - something`` does not -- it is
    handed to the episode expressions instead, which is how a folder name ends
    up read as a four-digit season number.
    """
    for part in reversed(list(parents)):
        found = _SEASON_FOLDER.match(part.strip())
        if found:
            number = found.group("number") or found.group("short")
            return int(number)
    return None


def parse_many(paths: Iterable[str | PurePath]) -> Iterator[Parse]:
    for path in paths:
        yield parse(path)


def is_clean(path: str | PurePath, season: int, episode: int) -> bool:
    """True when the path parses to exactly this season and episode, with no range.

    This is the check to run *before* a rename and again after it. "The server
    read something" is not the same as "the server read what was intended".
    """
    return parse(path).matches(season, episode)


def clean_name(
    series: str,
    season: int,
    episode: int,
    title: str | None = None,
    *,
    suffix: str = ".mkv",
    tag: str | None = None,
) -> str:
    """Build a name the expressions read unambiguously.

    The form is ``Series - SxxEyy - Title``: the marker suppresses the
    absolute expression, the separator survives every character class in the
    expressions, and the title stays human. A trailing tag is kept as
    provenance where one is wanted.

        >>> clean_name("Northwind", 1, 3, "The Quiet Harbour")
        'Northwind - S01E03 - The Quiet Harbour.mkv'
    """
    if season < 0 or episode < 0:
        raise ValueError("season and episode numbers are not negative")
    parts = [f"{series} - S{season:02d}E{episode:02d}"]
    if title:
        parts.append(title)
    name = " - ".join(parts)
    if tag:
        name = f"{name} {tag}"
    return f"{name}{suffix}"
