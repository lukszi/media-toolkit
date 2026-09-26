"""jfkit.cli -- the entry point for the server side.

It shares the switches the file-side tool defines, and its dispatch, so a
script that drives both learns one set: a configuration file, verbosity, a log
file, machine-readable logging, and --dry-run against --apply on everything
that writes.

Fifteen sub-commands. Six of them read and nothing else -- predicting how a
filename will be read, surveying what is in the library, asking which device
backs a path, finding items, listing an item's children and reading everybody's
watched state -- and they are the ones worth running first.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from mkvkit.cli import SubCommand, add_common_arguments, run

from . import __version__
from .commands import LIST_HELP, REGISTRARS, expand_lists
from .config import Config
from .naming import PARSED_AGAINST, VIDEO_SUFFIXES, Rule, parse

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
            "no expression claims, and a path too long for the classic "
            "Windows limit, are reported and do not change the exit status."
        ),
    )
    naming.add_argument("paths", nargs="+", metavar="PATH",
                        help="files, or folders to walk" + LIST_HELP)
    naming.add_argument(
        "--only-problems", action="store_true",
        help="print only the names that would be read as a range or not at all",
    )
    naming.add_argument(
        "--full-paths", action="store_true",
        help="print each path as given rather than its last segment, so batched "
             "output can be matched to its input",
    )
    naming.set_defaults(handler=_naming)


#: The classic Windows path limit, less the terminating character. A path
#: longer than this is a problem for every tool that has not opted out of it.
MAX_PATH = 259

#: What the server writes beside a video, relative to its path without the
#: extension. The trickplay tiles are the deepest: a folder, a resolution
#: folder, and a numbered picture.
SIDECAR_TAILS: tuple[str, ...] = (
    ".nfo", "-thumb.jpg", ".trickplay/320 - 10x10/000.jpg",
)


def _longest_with_sidecars(path: str) -> int:
    """The length of the longest path the server will derive from this one."""
    windows = "\\" in path or (len(path) > 1 and path[1] == ":")
    pure: PurePath = PureWindowsPath(path) if windows else PurePosixPath(path)
    stem = str(pure.with_suffix("")) if pure.suffix else str(pure)
    tails = [tail.replace("/", "\\") if windows else tail for tail in SIDECAR_TAILS]
    return max(len(path), *(len(stem) + len(tail) for tail in tails))


def _expand(values: Sequence[str]) -> list[str | Path]:
    """Folders walked for video files; anything else kept exactly as given."""
    out: list[str | Path] = []
    for value in expand_lists(values):
        path = Path(value)
        if path.is_dir():
            out += sorted(
                p for p in path.rglob("*")
                if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
            )
        else:
            out.append(value)
    return out


def _naming(args: argparse.Namespace, _config: Config) -> int:
    """Print one line per path, and exit non-zero if any would get a range.

    The exit code is the point: this is meant to be run in a pipeline before a
    rename, where "it printed something" is not a signal and an exit code is.
    """
    results = [parse(path) for path in _expand(args.paths)]
    shown = 0
    too_long = 0
    for result in results:
        longest = _longest_with_sidecars(result.path)
        long_path = longest > MAX_PATH
        too_long += long_path
        problem = result.would_get_end or result.rule is Rule.NONE or long_path
        if args.only_problems and not problem:
            continue
        shown += 1
        flag = "RANGE " if result.would_get_end else "      "
        shown_as = result.path if args.full_paths else Path(result.path).name
        print(f"{flag}{result.describe()}  {shown_as}")
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
    print(
        f"\n{len(results)} path(s), {shown} shown, {ranged} would be read as a "
        f"range, {unread} claimed by no expression, {too_long} too long with "
        f"their sidecars.\nChecked against "
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
