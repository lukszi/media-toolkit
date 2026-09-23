"""mkvkit.cli -- the entry point, and the switches every sub-command shares.

Sub-commands are argparse sub-parsers resolved through a registry, so a
sub-command whose package is not installed simply does not appear. At this
milestone the registry is empty: what it holds is a later milestone, what it
is has to exist first, because the shared switches (config file, verbosity,
log file, dry run) are the contract every sub-command is written against.

Every writing sub-command added here takes --dry-run (the default) and
--apply. There is no third state.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load
from .logging import configure_logging

__all__ = ["REGISTRY", "SubCommand", "add_common_arguments", "build_parser", "main"]

log = logging.getLogger(__name__)

#: name -> a function that adds its sub-parser to the sub-parsers action.
SubCommand = Callable[["argparse._SubParsersAction[argparse.ArgumentParser]"], None]
REGISTRY: dict[str, SubCommand] = {}


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """The switches every command in every package shares."""
    parser.add_argument(
        "--config", metavar="PATH", type=Path,
        help="configuration file; otherwise the discovery order is used",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="count", default=0)
    parser.add_argument("--log-file", metavar="PATH", type=Path)
    parser.add_argument(
        "--log-json", action="store_true",
        help="one JSON object per line, so a detached run can be parsed later",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mkvkit", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"mkvkit {__version__}")
    add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for register in REGISTRY.values():
        register(subparsers)
    return parser


def level_from(verbose: int, quiet: int) -> int:
    return max(logging.DEBUG, logging.INFO - 10 * verbose + 10 * quiet)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        level_from(args.verbose, args.quiet), file=args.log_file, json=args.log_json
    )
    if args.command is None:
        parser.print_help()
        return 2
    try:
        config: Config = load(args.config)
    except ConfigError as exc:
        # every problem at once, not the first one
        print(str(exc))
        return 2
    log.debug("configuration loaded from %s", config.source or "defaults")
    parser.error(f"{args.command}: not implemented yet")
    return 2
