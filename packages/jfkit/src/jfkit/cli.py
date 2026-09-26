"""jfkit.cli -- the entry point for the server side.

It shares the switches the file-side tool defines, and its dispatch, so a
script that drives both learns one set: a configuration file, verbosity, a log
file, machine-readable logging, and --dry-run against --apply on everything
that writes.

Sixteen sub-commands. Six of them read and nothing else -- predicting how a
filename will be read, surveying what is in the library, asking which device
backs a path, finding items, listing an item's children and reading everybody's
watched state -- and they are the ones worth running first.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from mkvkit.cli import SubCommand, add_common_arguments, run
from mkvkit.walk import walk

from . import __version__
from .commands import LIST_HELP, REGISTRARS, expand_lists
from .config import Config
from .naming import (
    MAX_PATH,
    PARSED_AGAINST,
    VIDEO_SUFFIXES,
    Rule,
    longest_derived_path,
    parse,
)

__all__ = ["REGISTRY", "build_parser", "main"]

log = logging.getLogger(__name__)

REGISTRY: dict[str, SubCommand] = {}


def _register_own() -> None:
    from .verbs import REGISTRARS as READ_VERBS

    REGISTRY.setdefault("naming", _register_naming)
    for name, register in {**REGISTRARS, **READ_VERBS}.items():
        REGISTRY.setdefault(name, register)


def _register_naming(
    subparsers: argparse._SubParsersAction,  # type: ignore[type-arg]
) -> None:
    naming = subparsers.add_parser(
        "naming", help="predict how a filename will be read, before renaming",
        description="Predict how the server will read each filename.",
        epilog=(
            "exit status: 0 when no name would be read as an episode range; "
            "1 when at least one would (a double episode named on purpose "
            "exits 1 too -- read the RANGE lines); 2 for a usage error. A name "
            "no expression claims, a file the extras rules make an extra "
            "(EXTRA), a folder name one or two letters away from an extras "
            "folder (WARN), and a path too long for the classic Windows limit "
            "are reported and do not change the exit status."
        ),
    )
    naming.add_argument("paths", nargs="+", metavar="PATH",
                        help="files, or folders to walk" + LIST_HELP)
    naming.add_argument(
        "--only-problems", action="store_true",
        help="print only the names that would be read as a range, not at all, "
             "as an extra, or that carry a warning or a long path",
    )
    naming.add_argument(
        "--absolute-order", action="store_true",
        help="the series is shown in absolute order, which leaves the optimistic "
             "digit-run expression out, as the server does",
    )
    naming.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="when walking a folder, leave out entries whose name or relative "
             "path matches; links and junctions are never followed",
    )
    naming.add_argument(
        "--full-paths", action="store_true",
        help="print each path as given rather than its last segment, so batched "
             "output can be matched to its input",
    )
    naming.set_defaults(handler=_naming)


def _longest_with_sidecars(path: str) -> int:
    """The length of the longest path the server will derive from this one."""
    return longest_derived_path(path)


def _expand(values: Sequence[str], exclude: Sequence[str] = ()) -> list[str | Path]:
    """Folders walked for video files; anything else kept exactly as given.

    The walk enters no link or junction (:mod:`mkvkit.walk`) and leaves out
    what ``exclude`` names; everything it left out is logged, so a folder
    that was not looked at is never mistaken for one that had no problems.
    """
    out: list[str | Path] = []
    for value in expand_lists(values):
        path = Path(value)
        if path.is_dir():
            tree = walk(path, exclude=exclude, suffixes=VIDEO_SUFFIXES, sizes=False)
            out += sorted(entry.path for entry in tree)
            for skipped in tree.skipped:
                log.warning("not walked: %s", skipped)
        else:
            out.append(value)
    return out


def _naming(args: argparse.Namespace, _config: Config) -> int:
    """Print one line per path, and exit non-zero if any would get a range.

    The exit code is the point: this is meant to be run in a pipeline before a
    rename, where "it printed something" is not a signal and an exit code is.
    """
    absolute = getattr(args, "absolute_order", False)
    results = [parse(path, absolute_order=absolute) for path in _expand(args.paths, args.exclude)]
    shown = 0
    too_long = 0
    for result in results:
        longest = _longest_with_sidecars(result.path)
        long_path = longest > MAX_PATH
        too_long += long_path
        problem = (result.would_get_end or result.rule is Rule.NONE or result.is_extra
                   or bool(result.warnings) or long_path)
        if args.only_problems and not problem:
            continue
        shown += 1
        flag = ("RANGE " if result.would_get_end else "EXTRA " if result.is_extra
                else "WARN  " if result.warnings else "      ")
        shown_as = result.path if args.full_paths else Path(result.path).name
        print(f"{flag}{result.describe()}  {shown_as}")
        for warning in result.warnings:
            print(f"        warning: {warning}")
        for note in result.notes:
            print(f"        note: {note}")
        if long_path:
            print(
                f"        note: LONG -- with the files the server writes beside it "
                f"the longest path is {longest} characters, over the {MAX_PATH} "
                "that many Windows programs can open"
            )
    ranged = sum(1 for result in results if result.would_get_end)
    unread = sum(1 for result in results if result.rule is Rule.NONE)
    extras = sum(1 for result in results if result.is_extra)
    warned = sum(1 for result in results if result.warnings)
    print(
        f"\n{len(results)} path(s), {shown} shown, {ranged} would be read as a "
        f"range, {unread} claimed by no expression, {extras} extra(s), {warned} "
        f"with a warning, {too_long} too long with their sidecars.\nChecked against "
        f"{PARSED_AGAINST}; see docs/gotchas/jellyfin-12.md."
    )
    return 1 if ranged else 0


def build_parser() -> argparse.ArgumentParser:
    _register_own()
    parser = argparse.ArgumentParser(prog="jfkit", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"jfkit {__version__}")
    add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for register in REGISTRY.values():
        register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser(), argv)
