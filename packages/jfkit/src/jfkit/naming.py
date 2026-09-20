#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Predict how Jellyfin 12.1 will parse an episode path -- BEFORE renaming.

Ports the two regex sets that decide IndexNumber / ParentIndexNumber /
IndexNumberEnd (Emby.Naming/Common/NamingOptions.cs, 12.1):

  * KODI_SXXEYY  - the FIRST entry of EpisodeExpressions.  Any name carrying an
    SxxEyy marker is claimed here, and it has no `endingepnumber` group.
  * ABSOLUTE_RANGE - the "Foo Bar 889" expression further down the same list.
    This is the one that turns `Hollowmere 1-05 ...` into the RANGE 1..5.
    It is guarded by `(?![^\\/]*[Ss][0-9]+[][ ._-]*[Ee][0-9]+)`, which is why
    adding an SxxEyy token is the durable cure (gotcha 13).
  * MULTI - all 10 MultipleEpisodeExpressions, which EpisodePathParser.
    FillAdditional runs over the path AFTER the primary match, and which can
    still attach an `endingepnumber` to an otherwise clean name.

A path is CLEAN when KODI_SXXEYY claims it with the intended S/E and neither
ABSOLUTE_RANGE nor MULTI yields an endingepnumber.

Usage:
    python check_episode_parse.py <path-or-directory> [...]
    ls *.avi | python check_episode_parse.py -

Exit code 1 if any input would receive an IndexNumberEnd.
"""
import os
import re
import sys

KODI_SXXEYY = re.compile(
    r".*(\\|\/)(?P<seriesname>((?![Ss]([0-9]+)[][ ._-]*[Ee]([0-9]+))[^\\\/])*)?"
    r"[Ss](?P<seasonnumber>[0-9]+)[][ ._-]*[Ee](?P<epnumber>[0-9]+)([^\\/]*)$")

ABSOLUTE_RANGE = re.compile(
    r".*[\\\/](?![Ee]pisode)(?![^\\\/]*[Ss][0-9]+[][ ._-]*[Ee][0-9]+)"
    r"(?P<seriesname>[\w\s]+?)\s(?P<epnumber>[0-9]{1,4})"
    r"(-(?P<endingepnumber>[0-9]{2,4}))*[^\\\/x]*$")

_MULTI_RAW = [
    r".*(\\|\/)[sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3})((-| - )[0-9]{1,4}[eExX](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)[sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3})((-| - )[0-9]{1,4}[xX][eE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)[sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3})((-| - )?[xXeE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)[sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3})(-[xE]?[eE]?(?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?([sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3}))((-| - )[0-9]{1,4}[xXeE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?([sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3}))((-| - )[0-9]{1,4}[xX][eE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?([sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3}))((-| - )?[xXeE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>((?![sS]?[0-9]{1,4}[xX][0-9]{1,3})[^\\\/])*)?([sS]?(?<seasonnumber>[0-9]{1,4})[xX](?<epnumber>[0-9]{1,3}))(-[xX]?[eE]?(?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>[^\\\/]*)[sS](?<seasonnumber>[0-9]{1,4})[xX\.]?[eE](?<epnumber>[0-9]{1,3})((-| - )?[xXeE](?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
    r".*(\\|\/)(?<seriesname>[^\\\/]*)[sS](?<seasonnumber>[0-9]{1,4})[xX\.]?[eE](?<epnumber>[0-9]{1,3})(-[xX]?[eE]?(?<endingepnumber>[0-9]{1,3}))+[^\\\/]*$",
]
MULTI = [re.compile(p.replace("(?<", "(?P<")) for p in _MULTI_RAW]

VIDEO = (".avi", ".mkv", ".mp4", ".m4v", ".mpg", ".mpeg", ".ts", ".wmv", ".mov")


def parse(path):
    """Return (season, episode, end, source) as Jellyfin would resolve them."""
    season = episode = end = None
    source = "-"
    m = KODI_SXXEYY.match(path)
    if m:
        season = int(m.group("seasonnumber"))
        episode = int(m.group("epnumber"))
        source = "SxxEyy"
    else:
        m = ABSOLUTE_RANGE.match(path)
        if m:
            episode = int(m.group("epnumber"))
            end = m.group("endingepnumber")
            end = int(end) if end else None
            source = "absolute"
    for p in MULTI:                       # FillAdditional, runs either way
        mm = p.match(path)
        if mm and mm.group("endingepnumber"):
            end = int(mm.group("endingepnumber"))
            source += "+multi"
            break
    return season, episode, end, source


def expand(args):
    for a in args:
        if os.path.isdir(a):
            for f in sorted(os.listdir(a)):
                if f.lower().endswith(VIDEO):
                    yield os.path.join(a, f)
        else:
            yield a


def main(argv):
    args = argv[1:]
    if not args:
        print(__doc__)
        return 2
    paths = [l.strip() for l in sys.stdin if l.strip()] if args == ["-"] else list(expand(args))
    bad = 0
    for p in paths:
        full = p if ("\\" in p or "/" in p) else os.path.join(os.getcwd(), p)
        full = full.replace("/", "\\")
        s, e, end, src = parse(full)
        flag = "RANGE!" if end is not None else "ok    "
        bad += end is not None
        print("%s S=%-4s E=%-4s end=%-5s [%-13s] %s"
              % (flag, s, e, end, src, os.path.basename(p)))
    print("\n%d path(s), %d would get an IndexNumberEnd" % (len(paths), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
