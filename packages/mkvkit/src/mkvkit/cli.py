"""mkvkit.cli -- the entry point, and the switches every sub-command shares.

Sub-commands are argparse sub-parsers resolved through a registry, so one
whose dependencies are absent simply does not appear rather than breaking the
whole program. Every sub-command that writes takes ``--dry-run`` (the default)
and ``--apply``. There is no third state.

The shared switches are the contract: a configuration file, verbosity, a log
file, and machine-readable logging. A script that drives both entry points
learns one set of them.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load
from .errors import ExtraRequired, ToolNotFound
from .logging import configure_logging

__all__ = [
    "REGISTRY",
    "Handler",
    "SubCommand",
    "add_common_arguments",
    "build_parser",
    "main",
    "run",
]

log = logging.getLogger(__name__)

#: A function that adds its sub-parser to the sub-parsers action.
SubCommand = Callable[["argparse._SubParsersAction[argparse.ArgumentParser]"], None]
#: What a sub-parser's ``handler`` default has to be.
Handler = Callable[[argparse.Namespace, Config], int]

REGISTRY: dict[str, SubCommand] = {}


def _register_own() -> None:
    """Register the sub-commands this package provides, if they can load."""
    from .commands import REGISTRARS
    from .langid.cli import register as langid

    for name, register in REGISTRARS.items():
        REGISTRY.setdefault(name, register)
    REGISTRY.setdefault("langid", langid)


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
    _register_own()
    parser = argparse.ArgumentParser(prog="mkvkit", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"mkvkit {__version__}")
    add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for register in REGISTRY.values():
        register(subparsers)
    return parser


def level_from(verbose: int, quiet: int) -> int:
    return max(logging.DEBUG, logging.INFO - 10 * verbose + 10 * quiet)


def run(parser: argparse.ArgumentParser, argv: Sequence[str] | None = None) -> int:
    """Parse, configure, dispatch -- shared by both entry points.

    The three failures a user actually hits get an exit code and one sentence,
    not a traceback: a configuration that cannot be used, a program that is not
    installed, and an optional dependency that is missing. Everything else is
    a bug and prints its traceback, because hiding those helps nobody.
    """
    args = parser.parse_args(argv)
    configure_logging(
        level_from(args.verbose, args.quiet), file=args.log_file, json=args.log_json
    )
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 2
    try:
        config: Config = load(args.config)
    except ConfigError as exc:
        # every problem at once, not the first one
        print(str(exc))
        return 2
    log.debug("configuration loaded from %s", config.source or "defaults")

    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"{args.command}: no verb given")
    try:
        return handler(args, config)
    except (ToolNotFound, ExtraRequired) as exc:
        print(str(exc))
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser(), argv)
