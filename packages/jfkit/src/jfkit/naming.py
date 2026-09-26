"""jfkit.naming -- predict how a media server will read a filename, before renaming.

Renaming an episode is cheap. Renaming it into a shape the server parses
differently is not: the item gets a new identity, its watch state does not
follow, and a file that parses as an episode *range* makes the metadata
provider concatenate the titles and plots of every episode in that range, on
every refresh, for ever. This module answers the question first, on a string,
with no server and no file.

**What it is.** A port of the naming code of Jellyfin 12.1 (the ``Emby.Naming``
assembly, recorded in :data:`PARSED_AGAINST` with the commit it was read at):

* all twenty-six episode expressions, in upstream order, with their flags
  (by-date, optimistic, named, absolute-capable) and the date formats of the
  two by-date ones -- :data:`EPISODE_EXPRESSIONS`;
* the ten multi-episode expressions that attach an end number afterwards --
  :data:`MULTIPLE_EPISODE_EXPRESSIONS`;
* the episode path parser around them (:func:`episode_path`): first match
  wins, the season-range guard, the end-number guards, and the "extended
  info" pass that fills a series name and an end number;
* the resolver's guards (:func:`parse`): only a video extension (or a
  ``.disc`` stub) is an episode at all, and anything the extras rules claim
  is an extra and never an episode;
* the extras rules (:data:`EXTRA_RULES`, :func:`extra_type`): fourteen folder
  names, three whole filenames and eighteen filename suffixes;
* the season-folder parser (:func:`season_folder`), with its keyword list.

Every expression is written out here in Python syntax, translated by hand
from the upstream source and cited by file and line beside it. ``(?<name>``
became ``(?P<name>``; everything else is upstream's text. They are compiled
case-insensitively, as upstream compiles them.

**What it is not.** It is somebody else's parser, reimplemented, and that is a
standing hazard: the day the upstream expressions change, this becomes
confidently wrong rather than obviously broken. Two things keep that honest:
the version it was checked against is recorded, and every behaviour is pinned
by tables translated from upstream's own naming tests (with invented names).
The server also does things around the parser that no string can predict --
which folder is the series, whether a folder resolved as a season -- and
those are reported as notes rather than guessed into the numbers.

**Behaviours worth knowing before you rename anything.**

*The first expression that matches wins, and the list is long.* A name with
no ``SxxEyy`` marker is tried against twenty-five more shapes: ``E16.``,
``ep01``, a bare ``1x03``, ``Name - 101``, ``01 - title``, a number and a
dot. A name that reads "unparsed" to a person is usually parsed by one of
them.

*A run of three or more digits after a separator is a season and an episode.*
The optimistic expression reads ``Name 8000 Title`` as season 80 episode 00,
and ``Name 8.000 Title`` as season 0 episode 0 -- a special. Seasons from 200
to 1927, and above 2500, are rejected outright, which is what keeps long
numeric ids from parsing as seasons.

*An ``SxxEyy`` token claims the name and suppresses the absolute expression.*
That is why adding one is the durable cure for a name that parses as a range.

*A bare number after a space is an absolute episode number,* and
``<name> 1-05 <junk>.avi`` is read as episode 1 with an end number of 5.
A four-digit year in a title is read as episode 1985.

*The end-number expressions run either way,* but only attach an end number
that is not smaller than the episode and is not followed by a digit, a ``p``
or an ``i`` (a resolution).

*A date in the name is a different kind of episode entirely.* Those get their
numbers re-derived from the path on every refresh, so an index number written
through the API does not survive.

*A file directly inside a folder called ``Featurettes`` (or any of the other
thirteen) is an extra, not an episode,* and a folder whose name is one or two
letters off is an ordinary folder whose files are episodes.

    >>> clean = "/srv/media/series/Northwind - S01E03 - The Quiet Harbour.mkv"
    >>> parse(clean).season, parse(clean).episode, parse(clean).end
    (1, 3, None)
    >>> ranged = parse("/srv/media/series/Harbour Lights 1-05 Pilot.avi")
    >>> ranged.episode, ranged.end, ranged.would_get_end
    (1, 5, True)
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePath, PurePosixPath, PureWindowsPath

__all__ = [
    "AUDIO_SUFFIXES",
    "EPISODE_EXPRESSIONS",
    "EXTRA_FOLDER_NAMES",
    "EXTRA_RULES",
    "MAX_PATH",
    "MULTIPLE_EPISODE_EXPRESSIONS",
    "PARSED_AGAINST",
    "SEASON_KEYWORDS",
    "SIDECAR_TAILS",
    "STUB_SUFFIXES",
    "UPSTREAM_COMMIT",
    "VIDEO_SUFFIXES",
    "EpisodeExpression",
    "EpisodePath",
    "ExtraRule",
    "Parse",
    "Rule",
    "SeasonFolder",
    "clean_name",
    "episode_path",
    "extra_type",
    "is_clean",
    "longest_derived_path",
    "near_miss_extra_folder",
    "parse",
    "parse_many",
    "season_folder",
]

log = logging.getLogger(__name__)

#: The commit of the upstream tag the expressions were read from.
UPSTREAM_COMMIT = "ee91c75e777da41a9c4f4855e70adc604fbf2ef8"

#: The release these expressions were read from and checked against. This is
#: the single most important line in the module: without it, a user cannot
#: tell whether the answer applies to the server they are running.
PARSED_AGAINST = f"Jellyfin 12.1 (Emby.Naming at {UPSTREAM_COMMIT[:10]}), September 2026"

_FLAGS = re.IGNORECASE

# ------------------------------------------------------------- file suffixes
#: Every suffix the server treats as a video file.
#: Emby.Naming/Common/NamingOptions.cs:24 (VideoFileExtensions).
VIDEO_SUFFIXES = frozenset({
    ".001", ".3g2", ".3gp", ".amv", ".asf", ".asx", ".avi", ".bin", ".bivx",
    ".divx", ".dv", ".dvr-ms", ".f4v", ".fli", ".flv", ".ifo", ".img", ".iso",
    ".m2t", ".m2ts", ".m2v", ".m4v", ".mkv", ".mk3d", ".mov", ".mp4", ".mpe",
    ".mpeg", ".mpg", ".mts", ".mxf", ".nrg", ".nsv", ".nuv", ".ogm", ".ogv",
    ".pva", ".qt", ".rec", ".rm", ".rmvb", ".strm", ".svq3", ".tp", ".ts", ".ty",
    ".viv", ".vob", ".vp3", ".webm", ".wmv", ".wtv", ".xvid",
})

#: A placeholder for a disc that is not on disk. Emby.Naming/Common/NamingOptions.cs:92.
STUB_SUFFIXES = frozenset({".disc"})

#: The stub types a ``.disc`` placeholder can name, by the token before the
#: suffix. Emby.Naming/Common/NamingOptions.cs:97 (StubTypes).
_STUB_TYPES: tuple[tuple[str, str], ...] = (
    ("dvd", "dvd"), ("hddvd", "hddvd"), ("bluray", "bluray"), ("brrip", "bluray"),
    ("bd25", "bluray"), ("bd50", "bluray"), ("vhs", "vhs"), ("hdtv", "tv"),
    ("pdtv", "tv"), ("dsr", "tv"),
)

#: Every suffix the server treats as audio; only the audio extras rules read it.
#: Emby.Naming/Common/NamingOptions.cs:212 (AudioFileExtensions).
AUDIO_SUFFIXES = frozenset({
    ".669", ".3gp", ".aa", ".aac", ".aax", ".ac3", ".act", ".adp", ".adplug",
    ".adx", ".afc", ".amf", ".aif", ".aifc", ".aiff", ".alac", ".amr", ".ape",
    ".ast", ".au", ".awb", ".cda", ".cue", ".dmf", ".dsf", ".dsm", ".dsp", ".dts",
    ".dvf", ".eac3", ".ec3", ".far", ".flac", ".gdm", ".gsm", ".gym", ".hps",
    ".imf", ".it", ".m15", ".m4a", ".m4b", ".mac", ".med", ".mka", ".mmf", ".mod",
    ".mogg", ".mp2", ".mp3", ".mpa", ".mpc", ".mpp", ".mp+", ".msv", ".nmf",
    ".nsf", ".nsv", ".oga", ".ogg", ".okt", ".opus", ".pls", ".ra", ".rf64",
    ".rm", ".s3m", ".sfx", ".shn", ".sid", ".stm", ".strm", ".ult", ".uni",
    ".vox", ".wav", ".wma", ".wv", ".xm", ".xsp", ".ymf",
})


# ---------------------------------------------------------------- the rules
class Rule(Enum):
    """Which kind of expression claimed the name (or why none did)."""

    #: An ``SxxEyy`` marker, or ``Season 3 Episode 9`` spelled out. The clean
    #: case, and the one to rename towards.
    SEASON_EPISODE = "season-episode"
    #: An episode number with no season: ``ep01``, ``E16.``, ``Episode 16``.
    EPISODE = "episode"
    #: A bare or hyphenated number, read as an absolute episode number.
    ABSOLUTE = "absolute"
    #: An ``NxNN`` form: season, a letter x, episode.
    CROSS = "cross"
    #: The optimistic digit-run expression: the last two digits of a run of
    #: three or more are the episode, the rest the season.
    OPTIMISTIC = "optimistic"
    #: ``part 2`` / ``pt.2``: an episode number from a part number.
    PART = "part"
    #: ``1-12 title``: two numbers joined by a hyphen, read as season and episode.
    PAIR = "pair"
    #: The number comes from a ``Season N/`` folder together with a leading
    #: number in the name, read across the separator.
    SEASON_FOLDER = "season-folder"
    #: A date in the name. The numbers are re-derived on every refresh and
    #: cannot be written durably.
    BY_DATE = "by-date"
    #: The extras rules claim the file: it is an extra, not an episode.
    EXTRA = "extra"
    #: Kept for callers written against the partial port, which reconstructed
    #: ``NxNN`` names from a multi-episode expression. The full port reads those
    #: with the upstream primary expression (:attr:`CROSS`) and never returns it.
    RECONSTRUCTED = "reconstructed"
    #: Nothing matched, or the file is not a video the server resolves.
    NONE = "none"


@dataclass(frozen=True)
class EpisodeExpression:
    """One upstream episode expression, with its flags and where it was read."""

    number: int
    source: str
    line: int
    rule: Rule
    by_date: bool = False
    optimistic: bool = False
    named: bool = False
    supports_absolute: bool = True
    date_formats: tuple[str, ...] = ()
    #: a character every match contains; a path without it is not searched.
    #: Only a shortcut -- the expression could not match without it anyway --
    #: taken because the bracket expressions backtrack heavily on deep paths.
    requires: str | None = None
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regex", re.compile(self.source, _FLAGS))


def _episode_expressions() -> tuple[EpisodeExpression, ...]:
    e = EpisodeExpression
    return (
        # Kodi: foo.s01.e01, foo.s01_e01, S01E02 foo, S01 - E02
        e(1, r".*(\\|\/)(?P<seriesname>((?![Ss]([0-9]+)[][ ._-]*[Ee]([0-9]+))[^\\\/])*)?"
             r"[Ss](?P<seasonnumber>[0-9]+)[][ ._-]*[Ee](?P<epnumber>[0-9]+)([^\\/]*)$",
          324, Rule.SEASON_EPISODE, named=True),
        # Kodi: foo.ep01, foo.EP_01
        e(2, r"[\._ -]()[Ee][Pp]_?([0-9]+)([^\\/]*)$", 329, Rule.EPISODE),
        # Kodi: foo.E01., foo.e01.
        e(3, r"[^\\/]*?()\.?[Ee]([0-9]+)\.([^\\/]*)$", 331, Rule.EPISODE),
        e(4, r"(?P<year>[0-9]{4})[._ -](?P<month>[0-9]{2})[._ -](?P<day>[0-9]{2})",
          332, Rule.BY_DATE, by_date=True,
          date_formats=("yyyy.MM.dd", "yyyy-MM-dd", "yyyy_MM_dd", "yyyy MM dd")),
        e(5, r"(?P<day>[0-9]{2})[._ -](?P<month>[0-9]{2})[._ -](?P<year>[0-9]{4})",
          342, Rule.BY_DATE, by_date=True,
          date_formats=("dd.MM.yyyy", "dd-MM-yyyy", "dd_MM_yyyy", "dd MM yyyy")),
        # a season and an episode spelled out, or "S03 E09" with a space between
        e(6, r".*[\\\/]((?P<seriesname>[^\\/]+?)\s)?[Ss](?:eason)?\s*(?P<seasonnumber>[0-9]+)"
             r"\s+[Ee](?:pisode)?\s*(?P<epnumber>[0-9]+).*$",
          356, Rule.SEASON_EPISODE, named=True),
        # "Foo Bar 889", guarded against a name that starts with "Episode" and
        # against an SxxEyy marker anywhere in the same segment
        e(7, r".*[\\\/](?![Ee]pisode)(?![^\\\/]*[Ss][0-9]+[][ ._-]*[Ee][0-9]+)"
             r"(?P<seriesname>[\w\s]+?)\s(?P<epnumber>[0-9]{1,4})"
             r"(-(?P<endingepnumber>[0-9]{2,4}))*[^\\\/x]*$",
          367, Rule.ABSOLUTE, named=True),
        e(8, r"[\\\/\._ \[\(-]([0-9]+)x([0-9]+(?:(?:[a-i]|\.[1-9])(?![0-9]))?)([^\\\/]*)$",
          372, Rule.CROSS),
        # "[bar] Foo - 1 [baz]"
        e(9, r".*[\\\/]?.*?(\[.*?\])+.*?(?P<seriesname>[-\w\s]+?)[\s_]*-[\s_]*"
             r"(?P<epnumber>[0-9]+).*$",
          379, Rule.ABSOLUTE, named=True, requires="["),
        # "Name - 101", "Name - 101 [720p]", "Name - 101 (2020)"
        e(10, r".*[\\\/](?P<seriesname>[^\\\/]+?)[\s_]+-[\s_]+(?P<epnumber>[0-9]+)[\s_]*"
              r"(?:\[.*?\]|\(.*?\))*[\s_]*(?:\.\w+)?$",
          387, Rule.ABSOLUTE, named=True),
        # "/server/anything_102": the optimistic digit run
        e(11, r"[\\/._ -](?P<seriesname>(?![0-9]+[0-9][0-9])([^\\\/_])*)[\\\/._ -]"
              r"(?P<seasonnumber>[0-9]+)(?P<epnumber>[0-9][0-9](?:(?:[a-i]|\.[1-9])(?![0-9]))?)"
              r"([._ -][^\\\/]*)$",
          395, Rule.OPTIMISTIC, optimistic=True, named=True, supports_absolute=False),
        e(12, r"[\/._ -]p(?:ar)?t[_. -]()([ivx]+|[0-9]+)([._ -][^\/]*)$", 401, Rule.PART),
        # "Episode 16", "Episode 16 - Title"
        e(13, r"[Ee]pisode (?P<epnumber>[0-9]+)(-(?P<endingepnumber>[0-9]+))?[^\\\/]*$",
          409, Rule.EPISODE, named=True),
        e(14, r".*(\\|\/)[sS]?(?P<seasonnumber>[0-9]+)[xX](?P<epnumber>[0-9]+)[^\\\/]*$",
          414, Rule.CROSS, named=True),
        e(15, r".*(\\|\/)[sS](?P<seasonnumber>[0-9]+)[x,X]?[eE](?P<epnumber>[0-9]+)[^\\\/]*$",
          419, Rule.SEASON_EPISODE, named=True),
        e(16, r".*(\\|\/)(?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?"
              r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]+))[^\\\/]*$",
          424, Rule.CROSS, named=True),
        e(17, r".*(\\|\/)(?P<seriesname>[^\\\/]*)[sS](?P<seasonnumber>[0-9]{1,4})[xX\.]?"
              r"[eE](?P<epnumber>[0-9]+)[^\\\/]*$",
          429, Rule.SEASON_EPISODE, named=True),
        # a bare number and the suffix, nothing else
        e(18, r".*[\\\/](?P<epnumber>[0-9]+)(-(?P<endingepnumber>[0-9]+))*\.\w+$",
          435, Rule.ABSOLUTE, optimistic=True, named=True),
        # "1-12 episode title"
        e(19, r"([0-9]+)-([0-9]+)", 442, Rule.PAIR),
        # "01 - blah", "01-blah"
        e(20, r".*(\\|\/)(?P<epnumber>[0-9]{1,3})(-(?P<endingepnumber>[0-9]{2,3}))*"
              r"\s?-\s?[^\\\/]*$",
          445, Rule.ABSOLUTE, optimistic=True, named=True),
        # a number, a dot and a title
        e(21, r".*(\\|\/)(?P<epnumber>[0-9]{1,3})(-(?P<endingepnumber>[0-9]{2,3}))*"
              r"\.[^\\\/]+$",
          452, Rule.ABSOLUTE, optimistic=True, named=True),
        # "blah - 01", "blah 2 - 01 blah", "blah - 01 - blah"
        e(22, r".*[\\\/][^\\\/]* - (?P<epnumber>[0-9]{1,3})(-(?P<endingepnumber>[0-9]{2,3}))*"
              r"[^\\\/]*$",
          459, Rule.ABSOLUTE, optimistic=True, named=True),
        # "Season 1/01 episode title"
        e(23, r"[Ss]eason[\._ ](?P<seasonnumber>[0-9]+)[\\\/](?P<epnumber>[0-9]{1,3})"
              r"([^\\\/]*)$",
          466, Rule.SEASON_FOLDER, optimistic=True, named=True),
        # series and season only: "the show/season 1", "the show/s01".
        # Upstream's leading ``(.*(\\|\/))*`` is written ``(...)?`` here and in
        # 25: it matches the same prefixes and finds the same first match (the
        # longest prefix that ends in a separator), but the starred form
        # backtracks exponentially in the number of folders in Python's engine.
        e(24, r"(.*(\\|\/))?(?P<seriesname>.+)\/[Ss](eason)?[\. _\-]*(?P<seasonnumber>[0-9]+)",
          474, Rule.SEASON_FOLDER, named=True),
        # series and season only: "the show S01", "the show season 1"
        e(25, r"(.*(\\|\/))?(?P<seriesname>.+)[\. _\-]+[sS](eason)?[\. _\-]*"
              r"(?P<seasonnumber>[0-9]+)",
          481, Rule.SEASON_FOLDER, named=True),
        # anime style: a bracketed group, a name, then the number in brackets
        e(26, r"(?:\[(?:[^\]]+)\]\s*)?(?P<seriesname>\[[^\]]+\]|[^[\]]+)\s*"
              r"\[(?P<epnumber>[0-9]+)\]",
          489, Rule.ABSOLUTE, named=True, requires="["),
    )


#: The twenty-six episode expressions of 12.1, in the order they are tried.
#: Emby.Naming/Common/NamingOptions.cs:320-493 (EpisodeExpressions); each
#: entry carries its own line.
EPISODE_EXPRESSIONS: tuple[EpisodeExpression, ...] = _episode_expressions()

_MULTI_SOURCES: tuple[tuple[str, int], ...] = (
    (r".*(\\|\/)[sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
     r"((-| - )[0-9]{1,4}[eExX](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 767),
    (r".*(\\|\/)[sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
     r"((-| - )[0-9]{1,4}[xX][eE](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 768),
    (r".*(\\|\/)[sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
     r"((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 769),
    (r".*(\\|\/)[sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3})"
     r"(-[xE]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 770),
    (r".*(\\|\/)(?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?"
     r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
     r"((-| - )[0-9]{1,4}[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 771),
    (r".*(\\|\/)(?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?"
     r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
     r"((-| - )[0-9]{1,4}[xX][eE](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 772),
    (r".*(\\|\/)(?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?"
     r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
     r"((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 773),
    (r".*(\\|\/)(?P<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?"
     r"([sS]?(?P<seasonnumber>[0-9]{1,4})[xX](?P<epnumber>[0-9]{1,3}))"
     r"(-[xX]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+[^\\\/]*$", 774),
    (r".*(\\|\/)(?P<seriesname>[^\\\/]*)[sS](?P<seasonnumber>[0-9]{1,4})[xX\.]?"
     r"[eE](?P<epnumber>[0-9]{1,3})((-| - )?[xXeE](?P<endingepnumber>[0-9]{1,3}))+"
     r"[^\\\/]*$", 775),
    (r".*(\\|\/)(?P<seriesname>[^\\\/]*)[sS](?P<seasonnumber>[0-9]{1,4})[xX\.]?"
     r"[eE](?P<epnumber>[0-9]{1,3})(-[xX]?[eE]?(?P<endingepnumber>[0-9]{1,3}))+"
     r"[^\\\/]*$", 776),
)

#: The ten multi-episode expressions, all "named". They never claim a name;
#: they only attach an end number (and a series name) to one that was claimed.
#: Emby.Naming/Common/NamingOptions.cs:765-779 (MultipleEpisodeExpressions).
MULTIPLE_EPISODE_EXPRESSIONS: tuple[EpisodeExpression, ...] = tuple(
    EpisodeExpression(number, source, line, Rule.NONE, named=True)
    for number, (source, line) in enumerate(_MULTI_SOURCES, start=1)
)


# ---------------------------------------------------------- .NET primitives
_INT = re.compile(r"[\t\n\v\f\r ]*[+-]?[0-9]+[\t\n\v\f\r ]*", re.ASCII)


def _try_int(text: str | None) -> int | None:
    """``int.TryParse(text, NumberStyles.Integer, InvariantCulture)``.

    ASCII digits only, surrounding white space and a sign allowed, and a
    32-bit range: a longer run fails the parse, as it does upstream.
    """
    if not text or not _INT.fullmatch(text):
        return None
    value = int(text)
    return value if -(2**31) <= value < 2**31 else None


def _file_name(path: str) -> str:
    """``Path.GetFileName``: the text after the last separator of either kind."""
    return re.split(r"[\\/]", path)[-1]


def _directory_name(path: str) -> str:
    """``Path.GetDirectoryName``: everything before the last separator."""
    cut = max(path.rfind("/"), path.rfind("\\"))
    return path[:cut] if cut > 0 else ("" if cut < 0 else path[:1])


def _extension(path: str) -> str:
    """``Path.GetExtension``: from the last dot of the file name, or nothing."""
    name = _file_name(path)
    dot = name.rfind(".")
    return "" if dot < 0 or dot == len(name) - 1 else name[dot:]


def _stem(path: str) -> str:
    """``Path.GetFileNameWithoutExtension``."""
    name = _file_name(path)
    dot = name.rfind(".")
    return name if dot < 0 else name[:dot]


# ------------------------------------------------------ the episode parser
@dataclass(frozen=True)
class EpisodePath:
    """What the episode path parser makes of one path (``EpisodePathParserResult``)."""

    success: bool = False
    season: int | None = None
    episode: int | None = None
    end: int | None = None
    series_name: str | None = None
    by_date: bool = False
    year: int | None = None
    month: int | None = None
    day: int | None = None
    #: which of :data:`EPISODE_EXPRESSIONS` claimed it, 1-based
    expression: int | None = None
    #: which of :data:`MULTIPLE_EPISODE_EXPRESSIONS` attached the end, 1-based
    multi_expression: int | None = None

    @property
    def date(self) -> dt.date | None:
        if self.year is None or self.month is None or self.day is None:
            return None
        return dt.date(self.year, self.month, self.day)


@dataclass
class _Result:
    success: bool = False
    season: int | None = None
    episode: int | None = None
    end: int | None = None
    series_name: str | None = None
    by_date: bool = False
    year: int | None = None
    month: int | None = None
    day: int | None = None


_DATE_TOKENS = {"yyyy": r"(?P<year>[0-9]{4})", "MM": r"(?P<month>[0-9]{2})",
                "dd": r"(?P<day>[0-9]{2})"}


def _parse_exact(text: str, formats: Sequence[str]) -> dt.date | None:
    """``DateTime.TryParseExact`` for the four-digit-year formats upstream uses."""
    for fmt in formats:
        pattern = re.escape(fmt)
        for token, group in _DATE_TOKENS.items():
            pattern = pattern.replace(token, group)
        found = re.fullmatch(pattern, text)
        if not found:
            continue
        try:
            return dt.date(int(found["year"]), int(found["month"]), int(found["day"]))
        except ValueError:
            continue
    return None


def _group(match: re.Match[str], name: str) -> str | None:
    """``match.Groups[name].Value`` -- an absent or unmatched group reads as nothing."""
    return match.groupdict().get(name)


def _parse_one(name: str, expression: EpisodeExpression) -> _Result:
    """One expression against one path. EpisodePathParser.cs:95-201."""
    result = _Result()
    if expression.by_date:
        # "a hack to handle wmc naming" -- EpisodePathParser.cs:99-103
        name = name.replace("_", "-")
    if expression.requires is not None and expression.requires not in name:
        return result
    match = expression.regex.search(name)
    # (Full)(Season)(Episode)(Extension) -- EpisodePathParser.cs:108
    if not match or expression.regex.groups + 1 < 3:
        return result
    if expression.by_date:
        date = _parse_exact(match.group(0), expression.date_formats)
        if date is not None:
            result.year, result.month, result.day = date.year, date.month, date.day
        # upstream marks success whether or not the date parsed (its own TODO)
        result.success = True
    elif expression.named:
        result.season = _try_int(_group(match, "seasonnumber"))
        result.episode = _try_int(_group(match, "epnumber"))
        ending = _group(match, "endingepnumber")
        if ending is not None:
            # an end number followed by a digit, a "p" or an "i" is the start
            # of a resolution, not a range -- EpisodePathParser.cs:151-168
            after = match.end("endingepnumber")
            if after >= len(name) or name[after] not in "0123456789iIpP":
                number = _try_int(ending)
                if (number is not None and result.episode is not None
                        and number >= result.episode):
                    result.end = number
        result.series_name = _group(match, "seriesname") or ""
        result.success = result.episode is not None
    else:
        result.season = _try_int(match.group(1))
        result.episode = _try_int(match.group(2))
        result.success = result.episode is not None

    # seasons 200-1927 and above 2500 are rejected -- EpisodePathParser.cs:188-195
    if result.season is not None and (200 <= result.season < 1928 or result.season > 2500):
        result.success = False
    result.by_date = expression.by_date
    return result


def episode_path(
    path: str,
    *,
    is_directory: bool = False,
    is_named: bool | None = None,
    is_optimistic: bool | None = None,
    supports_absolute: bool | None = None,
    fill_extended_info: bool = True,
) -> EpisodePath:
    """``EpisodePathParser.Parse``, faithfully: Emby.Naming/TV/EpisodePathParser.cs:35-93.

    The whole path is searched, not only its last segment: most expressions
    anchor themselves to the last separator, but the by-date expressions and
    the ``1-12`` pair do not, and a folder name can satisfy them.
    """
    if is_directory:
        # "to be able to use regex patterns which require a file extension"
        path += ".mp4"

    claimed: _Result | None = None
    number: int | None = None
    for expression in EPISODE_EXPRESSIONS:
        if supports_absolute is not None and expression.supports_absolute != supports_absolute:
            continue
        if is_named is not None and expression.named != is_named:
            continue
        if is_optimistic is not None and expression.optimistic != is_optimistic:
            continue
        current = _parse_one(path, expression)
        if current.success:
            claimed, number = current, expression.number
            break

    if claimed is None:
        return EpisodePath()

    multi: int | None = None
    if fill_extended_info:
        multi = _fill_additional(path, claimed)
        if claimed.series_name:
            claimed.series_name = claimed.series_name.strip().strip("_.-").strip()

    return EpisodePath(
        success=True, season=claimed.season, episode=claimed.episode, end=claimed.end,
        series_name=claimed.series_name, by_date=claimed.by_date, year=claimed.year,
        month=claimed.month, day=claimed.day, expression=number, multi_expression=multi,
    )


def _fill_additional(path: str, info: _Result) -> int | None:
    """EpisodePathParser.cs:203-242: a series name and an end number, if missing.

    Returns the number of the multi-episode expression that attached an end
    number, if one did.
    """
    expressions: list[tuple[EpisodeExpression, bool]] = [
        (expression, True) for expression in MULTIPLE_EPISODE_EXPRESSIONS if expression.named
    ]
    if not info.series_name:
        expressions[:0] = [(e, False) for e in EPISODE_EXPRESSIONS if e.named]
    attached: int | None = None
    for expression, is_multi in expressions:
        result = _parse_one(path, expression)
        if not result.success:
            continue
        if not info.series_name:
            info.series_name = result.series_name
        if (info.end is None and result.end is not None and info.episode is not None
                and result.end >= info.episode):
            info.end = result.end
            if is_multi:
                attached = expression.number
        if info.series_name and (info.episode is None or info.end is not None):
            break
    return attached


# ------------------------------------------------------------------ extras
@dataclass(frozen=True)
class ExtraRule:
    """One upstream extras rule (``ExtraRule``)."""

    #: the upstream ``ExtraType`` name: Trailer, Featurette, Unknown, ...
    extra_type: str
    #: "directory", "filename" or "suffix"
    kind: str
    token: str
    #: "video" or "audio": which files the rule applies to at all
    media: str
    line: int


def _extra_rules() -> tuple[ExtraRule, ...]:
    r = ExtraRule
    return (
        r("Trailer", "directory", "trailers", "video", 497),
        r("ThemeVideo", "directory", "backdrops", "video", 503),
        r("ThemeSong", "directory", "theme-music", "audio", 509),
        r("BehindTheScenes", "directory", "behind the scenes", "video", 515),
        r("DeletedScene", "directory", "deleted scenes", "video", 521),
        r("Interview", "directory", "interviews", "video", 527),
        r("Scene", "directory", "scenes", "video", 533),
        r("Sample", "directory", "samples", "video", 539),
        r("Short", "directory", "shorts", "video", 545),
        r("Featurette", "directory", "featurettes", "video", 551),
        r("Unknown", "directory", "extras", "video", 557),
        r("Unknown", "directory", "extra", "video", 563),
        r("Unknown", "directory", "other", "video", 569),
        r("Clip", "directory", "clips", "video", 575),
        r("Trailer", "filename", "trailer", "video", 581),
        r("Sample", "filename", "sample", "video", 587),
        r("ThemeSong", "filename", "theme", "audio", 593),
        r("Trailer", "suffix", "-trailer", "video", 599),
        r("Trailer", "suffix", ".trailer", "video", 605),
        r("Trailer", "suffix", "_trailer", "video", 611),
        r("Trailer", "suffix", "- trailer", "video", 617),
        r("Sample", "suffix", "-sample", "video", 623),
        r("Sample", "suffix", ".sample", "video", 629),
        r("Sample", "suffix", "_sample", "video", 635),
        r("Sample", "suffix", "- sample", "video", 641),
        r("Scene", "suffix", "-scene", "video", 647),
        r("Clip", "suffix", "-clip", "video", 653),
        r("Interview", "suffix", "-interview", "video", 659),
        r("BehindTheScenes", "suffix", "-behindthescenes", "video", 665),
        r("DeletedScene", "suffix", "-deleted", "video", 671),
        r("DeletedScene", "suffix", "-deletedscene", "video", 677),
        r("Featurette", "suffix", "-featurette", "video", 683),
        r("Short", "suffix", "-short", "video", 689),
        r("Unknown", "suffix", "-extra", "video", 695),
        r("Unknown", "suffix", "-other", "video", 701),
    )


#: The extras rules of 12.1, in the order they are tried; the first match wins.
#: Emby.Naming/Common/NamingOptions.cs:495-706 (VideoExtraRules).
EXTRA_RULES: tuple[ExtraRule, ...] = _extra_rules()

#: Folder names that make every file directly inside them an extra.
#: Emby.Naming/Common/NamingOptions.cs:708 (AllExtrasTypesFolderNames).
EXTRA_FOLDER_NAMES: frozenset[str] = frozenset(
    rule.token for rule in EXTRA_RULES if rule.kind == "directory"
)


def extra_type(path: str, *, library_root: str = "") -> ExtraRule | None:
    """``ExtraRuleResolver.GetExtraInfo``: the rule that makes this file an extra.

    Emby.Naming/Video/ExtraRuleResolver.cs:23-67. Only the **immediate** folder
    counts for the folder rules, and a folder that is itself the library root
    does not. Trailing digits are trimmed before the suffix rules, so
    ``-trailer2`` is a trailer; the whole-name rules compare the name without
    its suffix exactly.
    """
    suffix = _extension(path).lower()
    is_audio = suffix in AUDIO_SUFFIXES
    is_video = suffix in VIDEO_SUFFIXES
    stem = _stem(path)
    trimmed = stem.rstrip("0123456789")
    full_directory = _directory_name(path)
    directory = _file_name(full_directory)
    for rule in EXTRA_RULES:
        if (rule.media == "audio" and not is_audio) or (rule.media == "video" and not is_video):
            continue
        token = rule.token.casefold()
        if rule.kind == "filename":
            hit = stem.casefold() == token
        elif rule.kind == "suffix":
            hit = trimmed.casefold().endswith(token)
        else:
            hit = (directory.casefold() == token
                   and full_directory.casefold() != library_root.casefold())
        if hit:
            return rule
    return None


def _distance(a: str, b: str) -> int:
    """Levenshtein distance, for the near-miss warning."""
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, start=1):
        current = [i]
        for j, right in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right),
            ))
        previous = current
    return previous[-1]


def near_miss_extra_folder(name: str) -> str | None:
    """The extras folder name this one is one or two edits away from, if any.

    Not upstream behaviour -- upstream compares exactly, and that is the point:
    a folder called ``Featurete`` is an ordinary folder, its files are read as
    episodes, and one of them takes an episode slot. This names the rule the
    folder was probably meant to match.
    """
    folded = name.strip().casefold()
    if not folded or folded in EXTRA_FOLDER_NAMES:
        return None
    best: tuple[int, str] | None = None
    for token in sorted(EXTRA_FOLDER_NAMES):
        # a two-letter slip in a five-letter word is a different word
        allowed = 1 if len(token) <= 5 else 2
        distance = _distance(folded, token)
        if distance <= allowed and (best is None or distance < best[0]):
            best = (distance, token)
    return None if best is None else best[1]


# ----------------------------------------------------------- season folders
#: The keywords a season folder may carry. Emby.Naming/TV/SeasonPathParser.cs:13-16.
SEASON_KEYWORDS: tuple[str, ...] = (
    "시즌", "シーズン", "сезон",
    "season", "sæson", "saison", "staffel", "series", "stagione", "säsong", "seizoen",
    "seasong", "sezon", "sezona", "sezóna", "sezonul", "série", "séria", "serie",
    "seria", "temporada", "kausi",
)
_KEYWORDS = "|".join(SEASON_KEYWORDS)
_CLEAN_NAME = re.compile(r"[ ._\-\[\]]")
# SeasonPathParser.cs:20
_PROCESS_PRE = re.compile(
    r"^\s*((?P<seasonnumber>(?>\d+))(?:st|nd|rd|th|\.)*(?!\s*[Ee]\d+))\s*(?:"
    + _KEYWORDS + r")\s*(?P<rightpart>.*)$", re.IGNORECASE,
)
# SeasonPathParser.cs:23
_PROCESS_POST = re.compile(
    r"^\s*(?:" + _KEYWORDS + r")\s*(?P<seasonnumber>\d+?)(?=\d{3,4}p|[^\d]|$)"
    r"(?!\s*[Ee]\d)(?P<rightpart>.*)$", re.IGNORECASE,
)
# SeasonPathParser.cs:26, case-sensitive upstream
_SEASON_PREFIX = re.compile(r"[sS](\d{1,4})(?!\d|[eE]\d)(?=\.|_|-|\[|\]|\s|$)")
_SEASON_KEYWORD = re.compile(_KEYWORDS, re.IGNORECASE)


@dataclass(frozen=True)
class SeasonFolder:
    """What the season-folder parser makes of one folder (``SeasonPathParserResult``)."""

    season: int | None = None
    is_season_folder: bool = False

    @property
    def success(self) -> bool:
        return self.season is not None


def season_folder(
    path: str,
    parent_path: str | None = None,
    *,
    special_aliases: bool = True,
    numeric_folders: bool = True,
) -> SeasonFolder:
    """``SeasonPathParser.Parse``: Emby.Naming/TV/SeasonPathParser.cs:40-137.

    The server calls it with both switches on for a folder under a series
    (``SeasonResolver``), so ``Specials``, ``Extras`` and a bare ``2`` are
    seasons there. ``parent_path`` is the series folder: its name is removed
    from the folder name first, so ``Northwind Season 2`` under ``Northwind``
    is season 2.
    """
    name = _file_name(path)
    prefix = _SEASON_PREFIX.search(name)
    if prefix:
        value = _try_int(prefix.group(1))
        if value is not None:
            return SeasonFolder(value, True)

    cleaned = _CLEAN_NAME.sub("", name)
    if parent_path is not None:
        parent = _CLEAN_NAME.sub("", _file_name(parent_path.rstrip("\\/")) or parent_path)
        if parent:
            cleaned = re.sub(re.escape(parent), "", cleaned, flags=re.IGNORECASE)

    if special_aliases and cleaned.casefold() in ("specials", "extras"):
        return SeasonFolder(0, True)
    if numeric_folders:
        value = _try_int(cleaned)
        if value is not None:
            return SeasonFolder(value, True)

    mixed = not numeric_folders and not special_aliases
    found = _PROCESS_PRE.search(cleaned)
    if found:
        if mixed and not _SEASON_KEYWORD.search(name):
            return SeasonFolder()
    else:
        found = _PROCESS_POST.search(cleaned)
        if found and mixed and not _SEASON_KEYWORD.search(name):
            return SeasonFolder()
    if found:
        value = _try_int(found.group("seasonnumber"))
        if value is not None:
            return SeasonFolder(value, True)
    return SeasonFolder()


# --------------------------------------------------------- path lengths
#: The classic Windows path limit, less the terminating character. A path
#: longer than this is a problem for every tool that has not opted out of it.
MAX_PATH = 259

#: What the server writes beside a video, relative to its path without the
#: extension. The preview tiles are the deepest: a folder, a resolution
#: folder, and a numbered picture.
SIDECAR_TAILS: tuple[str, ...] = (
    ".nfo", "-thumb.jpg", ".trickplay/320 - 10x10/000.jpg",
)


def longest_derived_path(path: str | PurePath) -> int:
    """The length of the longest path the server will write beside this one."""
    text = str(path)
    windows = "\\" in text or (len(text) > 1 and text[1] == ":")
    pure: PurePath = PureWindowsPath(text) if windows else PurePosixPath(text)
    stem = str(pure.with_suffix("")) if pure.suffix else str(pure)
    tails = [tail.replace("/", "\\") if windows else tail for tail in SIDECAR_TAILS]
    return max(len(text), *(len(stem) + len(tail) for tail in tails))


# ----------------------------------------------------------- the verdict
@dataclass(frozen=True)
class Parse:
    """What the server will make of one path."""

    path: str
    #: the season the name itself carries (``ParentIndexNumber`` from the path)
    season: int | None = None
    episode: int | None = None
    end: int | None = None
    rule: Rule = Rule.NONE
    #: a multi-episode expression attached the end number
    multi: bool = False
    notes: tuple[str, ...] = ()
    #: which of :data:`EPISODE_EXPRESSIONS` claimed the name, 1-based
    expression: int | None = None
    series_name: str | None = None
    #: the air date a by-date name carries, when it is a valid date
    date: dt.date | None = None
    #: the upstream extra type (``Featurette``, ``Trailer``, ...) for an extra
    extra: str | None = None
    #: a ``.disc`` placeholder rather than a video
    stub: bool = False
    #: the claiming expression is one upstream marks optimistic
    optimistic: bool = False
    #: the season a season folder above the file announces, if any
    season_from_folder: int | None = None
    #: things that are probably not what was meant (a near-miss extras folder)
    warnings: tuple[str, ...] = ()

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

    @property
    def is_extra(self) -> bool:
        return self.extra is not None

    @property
    def effective_season(self) -> int | None:
        """The season the server ends up with: the name's, else the folder's."""
        return self.season if self.season is not None else self.season_from_folder

    def matches(self, season: int | None, episode: int | None) -> bool:
        return self.season == season and self.episode == episode and self.end is None

    def describe(self) -> str:
        if self.extra is not None:
            return f"extra ({self.extra.lower()}), not an episode [{self.rule.value}]"
        parts = [
            f"S={self.season if self.season is not None else '-'}",
            f"E={self.episode if self.episode is not None else '-'}",
            f"end={self.end if self.end is not None else '-'}",
            f"[{self.rule.value}{'+multi' if self.multi else ''}]",
        ]
        return " ".join(parts)


def _anchored(text: str) -> str:
    """Give a bare name the directory every upstream path has.

    The server always parses a full path, and most expressions begin by
    consuming directories, so a bare basename would match nothing. One
    separator in front is the smallest path there is, and it does not make the
    answer depend on where the tool was run.
    """
    return text if ("/" in text or "\\" in text) else "/" + text


def _folders(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]", path)[:-1] if p]


def parse(path: str | PurePath, *, absolute_order: bool = False) -> Parse:
    """Predict season, episode and end number for one video path.

    This is ``EpisodeResolver.Resolve`` with the library's guards around it:
    a file that is not a video (or a ``.disc`` placeholder) is not an episode;
    a file the extras rules claim is an extra, never an episode
    (``Library/Resolvers/TV/EpisodeResolver.cs:58-64``); a season the name
    does not carry comes from the season folder
    (``LibraryManager.FillMissingEpisodeNumbersFromPath``), reported in
    :attr:`Parse.season_from_folder`. ``absolute_order`` is a series whose
    display order is "absolute", which leaves the optimistic digit-run
    expression out.
    """
    text = str(path)
    anchored = _anchored(text)
    folders = _folders(text)
    notes: list[str] = []
    warnings: list[str] = []

    if folders:
        near = near_miss_extra_folder(folders[-1])
        if near is not None:
            warnings.append(
                f"the folder {folders[-1]!r} is not an extras folder -- it is "
                f"{_distance(folders[-1].casefold(), near)} letter(s) away from "
                f"{near!r} -- so its files are read as episodes"
            )
        higher = [f for f in folders[:-1] if f.casefold() in EXTRA_FOLDER_NAMES]
        if higher and not warnings:
            notes.append(
                f"the folder {higher[-1]!r} further up makes nothing an extra: only "
                "the folder a file sits in directly counts"
            )

    suffix = _extension(text).lower()
    stub = False
    if suffix not in VIDEO_SUFFIXES:
        if suffix not in STUB_SUFFIXES:
            notes.append(
                f"{suffix or 'no suffix'}: not a video file the server resolves, so "
                "it is not an episode at all"
            )
            return Parse(path=text, notes=tuple(notes), warnings=tuple(warnings))
        stub = True
        notes.append("a .disc placeholder: an episode even when no number parses")

    extra = extra_type(anchored)
    if extra is not None:
        where = {"directory": f"the folder {extra.token!r}",
                 "filename": f"the name {extra.token!r}",
                 "suffix": f"the ending {extra.token!r}"}[extra.kind]
        notes.append(
            f"{where} makes this an extra ({extra.extra_type}); the server never "
            "reads it as an episode"
        )
        return Parse(path=text, rule=Rule.EXTRA, extra=extra.extra_type, stub=stub,
                     notes=tuple(notes), warnings=tuple(warnings))

    found = episode_path(anchored, supports_absolute=True if absolute_order else None)
    if not found.success and len(folders) >= 2:
        parent_is_season = season_folder(folders[-1], folders[-2]).is_season_folder
        above_is_season = (len(folders) >= 3
                           and season_folder(folders[-2], folders[-3]).is_season_folder)
        if not parent_is_season and above_is_season:
            # LibraryManager.cs:3309-3319: a file in a plain folder inside a
            # season is read from that folder's name instead
            folder_path = _directory_name(anchored)
            from_folder = episode_path(
                folder_path, is_directory=True,
                supports_absolute=True if absolute_order else None,
            )
            if from_folder.success:
                found = from_folder
                notes.append(
                    f"the name claims nothing, so the numbers come from its folder "
                    f"{folders[-1]!r}"
                )

    expression = (EPISODE_EXPRESSIONS[found.expression - 1]
                  if found.expression is not None else None)
    rule = expression.rule if expression is not None else Rule.NONE
    optimistic = bool(expression and expression.optimistic)
    date = found.date

    if found.by_date:
        notes.append(
            "a date in the name makes this a by-date episode: index numbers are "
            "re-derived from the path on every refresh and cannot be written durably"
        )
    elif rule is Rule.ABSOLUTE:
        notes.append(
            "read as an absolute episode number; adding an SxxEyy token to the "
            "name suppresses this expression"
        )
    elif rule is Rule.OPTIMISTIC:
        notes.append(
            "read by the optimistic expression: the last two digits of the number "
            "are the episode and the digits before them the season"
        )
    if optimistic and rule is not Rule.OPTIMISTIC:
        notes.append("claimed by an expression upstream marks optimistic")

    if found.end is not None:
        notes.append(
            "this name produces an episode range; the end number is refilled from "
            "the path on any refresh that runs a provider, so only a rename clears it"
        )

    folder_season: int | None = None
    if found.success and found.season is None:
        folder_season = _season_from_folders(folders)
        if folder_season is not None:
            notes.append(
                f"the parent folder names season {folder_season}; the expressions "
                "never read it, but the server does"
            )
        elif found.episode is not None or found.by_date:
            notes.append(
                "no season in the name or its folder: a file directly in the series "
                "folder is put in season 1"
            )

    return Parse(
        path=text, season=found.season, episode=found.episode, end=found.end, rule=rule,
        multi=found.multi_expression is not None, notes=tuple(notes),
        expression=found.expression, series_name=found.series_name or None, date=date,
        stub=stub, optimistic=optimistic, season_from_folder=folder_season,
        warnings=tuple(warnings),
    )


def _season_from_folders(folders: Sequence[str]) -> int | None:
    """The season the nearest season folder above the file announces.

    Each folder is judged the way ``SeasonResolver`` judges it, against the
    folder above it standing in for the series. A folder has to name a season
    with a keyword, an ``S01`` prefix, a bare number or the word ``Specials``;
    ``01 - something`` is not one.
    """
    for index in range(len(folders) - 1, 0, -1):
        found = season_folder(folders[index], folders[index - 1])
        if found.is_season_folder and found.season is not None:
            return found.season
    return None


def parse_many(
    paths: Iterable[str | PurePath], *, absolute_order: bool = False
) -> Iterator[Parse]:
    for path in paths:
        yield parse(path, absolute_order=absolute_order)


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

    The form is ``Series - SxxEyy - Title``: the marker is claimed by the first
    expression, the separator survives every character class in the
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
