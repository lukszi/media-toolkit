"""jfkit.cli -- the entry point for the server side.

It shares the switches the file-side tool defines, and its dispatch, so a
script that drives both learns one set: a configuration file, verbosity, a log
file, machine-readable logging, and --dry-run against --apply on everything
that writes.

One sub-command so far, and it is a read-only one: ``naming`` predicts how a
media server will read a filename, which is the cheapest thing in this
repository to run and the one worth running before a rename rather than after.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from mkvkit.cli import SubCommand, add_common_arguments, run

from . import __version__
from .config import Config
from .naming import PARSED_AGAINST, VIDEO_SUFFIXES, Rule, parse

__all__ = ["REGISTRY", "build_parser", "main"]

log = logging.getLogger(__name__)

REGISTRY: dict[str, SubCommand] = {}


def _register_own() -> None:
    REGISTRY.setdefault("naming", _register_naming)


def _register_naming(
    subparsers: argparse._SubParsersAction,  # type: ignore[type-arg]
) -> None:
    naming = subparsers.add_parser(
        "naming", help="predict how a filename will be read, before renaming"
    )
    naming.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    naming.add_argument(
        "--only-problems", action="store_true",
        help="print only the names that would be read as a range or not at all",
    )
    naming.set_defaults(handler=_naming)


def _expand(paths: Sequence[Path]) -> list[Path]:
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out += sorted(
                p for p in path.rglob("*")
                if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
            )
        else:
            out.append(path)
    return out


def _naming(args: argparse.Namespace, _config: Config) -> int:
    """Print one line per path, and exit non-zero if any would get a range.

    The exit code is the point: this is meant to be run in a pipeline before a
    rename, where "it printed something" is not a signal and an exit code is.
    """
    results = [parse(path) for path in _expand(args.paths)]
    shown = 0
    for result in results:
        problem = result.would_get_end or result.rule is Rule.NONE
        if args.only_problems and not problem:
            continue
        shown += 1
        flag = "RANGE " if result.would_get_end else "      "
        print(f"{flag}{result.describe()}  {Path(result.path).name}")
        for note in result.notes:
            print(f"        note: {note}")
    ranged = sum(1 for result in results if result.would_get_end)
    unread = sum(1 for result in results if result.rule is Rule.NONE)
    print(
        f"\n{len(results)} path(s), {shown} shown, {ranged} would be read as a "
        f"range, {unread} claimed by no expression.\nChecked against "
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
