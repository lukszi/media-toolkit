"""jfkit.cli -- the entry point for the server side.

It shares the switches the file-side tool defines, so a script that drives
both learns one set: a configuration file, verbosity, a log file, a JSON-lines
log, and --dry-run against --apply on everything that writes.

The registry is empty at this milestone. The contract is not.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from mkvkit.cli import SubCommand, add_common_arguments, level_from
from mkvkit.logging import configure_logging

from . import __version__
from .config import Config, ConfigError, load

__all__ = ["REGISTRY", "build_parser", "main"]

log = logging.getLogger(__name__)

REGISTRY: dict[str, SubCommand] = {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jfkit", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"jfkit {__version__}")
    add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for register in REGISTRY.values():
        register(subparsers)
    return parser


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
        print(str(exc))
        return 2
    log.debug("configuration loaded from %s", config.source or "defaults")
    parser.error(f"{args.command}: not implemented yet")
    return 2
