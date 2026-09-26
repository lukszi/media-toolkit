"""jfkit.verbs -- the read verbs.

``find``, ``children`` and ``playstate`` only read. They print rows as
tab-separated text (the default, with a header line) or as JSON, so they pipe
into whatever reads them next -- a mapping for a replay, a list for
``refresh``.

``playstate`` takes ``--jobs N``: how many requests
run at once, default :data:`mkvkit.lanes.DEFAULT_WORKERS`.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from typing import Any

from mkvkit.lanes import DEFAULT_WORKERS

from . import query
from .commands import LIST_HELP, client_from, expand_lists
from .config import Config

__all__ = ["REGISTRARS"]

log = logging.getLogger(__name__)


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=["tsv", "json"], default="tsv",
                        help="tab-separated with a header line (the default), or JSON")


def _add_jobs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"requests sent at once (default {DEFAULT_WORKERS})",
    )


# ------------------------------------------------------------------ find
def _register_find(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "find", help="items by name, path, provider id or type",
        description="Find items, as the configured user sees them.",
        epilog="exit status: 0 when something matched, 1 when nothing did.",
    )
    parser.add_argument("--name", help="part of the name, any case")
    parser.add_argument("--exact", action="store_true", help="the whole name, any case")
    parser.add_argument("--path", metavar="GLOB",
                        help="a glob against the path, or a folder: everything under it")
    parser.add_argument("--provider", metavar="KEY=VALUE",
                        help="a provider id, as Tmdb=1001, or a bare value for any provider")
    parser.add_argument("--type", action="append", default=[], dest="types",
                        metavar="TYPE", help="Series, Season, Episode, Movie, ...")
    parser.add_argument("--parent", metavar="ID", help="only below this item")
    _add_output(parser)
    parser.set_defaults(handler=_find)


def _find(args: argparse.Namespace, config: Config) -> int:
    if not any((args.name, args.path, args.provider, args.types, args.parent)):
        print("find needs at least one of --name, --path, --provider, --type, --parent")
        return 2
    rows = query.find(
        client_from(args, config), name=args.name, exact=args.exact, path=args.path,
        provider=args.provider, types=args.types, parent=args.parent,
    )
    print(query.render([query.item_row(r) for r in rows], query.ITEM_COLUMNS, args.format))
    return 0 if rows else 1


# -------------------------------------------------------------- children
def _register_children(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "children", help="an item's children, descendants or extras",
    )
    parser.add_argument("item_id")
    parser.add_argument("--recursive", action="store_true",
                        help="every descendant, not only the direct children")
    parser.add_argument("--type", action="append", default=[], dest="types",
                        metavar="TYPE")
    parser.add_argument("--extras", action="store_true",
                        help="the item's extras (trailers, featurettes, ...) instead")
    _add_output(parser)
    parser.set_defaults(handler=_children)


def _children(args: argparse.Namespace, config: Config) -> int:
    rows = query.children(client_from(args, config), args.item_id,
                          recursive=args.recursive, types=args.types, extras=args.extras)
    print(query.render([query.item_row(r) for r in rows], query.ITEM_COLUMNS, args.format))
    return 0


# ------------------------------------------------------------- playstate
def _register_playstate(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "playstate", help="every user's watched state for items",
        epilog="exit status: 0 when every item was read for every user, 1 otherwise.",
    )
    parser.add_argument("item_ids", nargs="+", metavar="ID", help="item ids" + LIST_HELP)
    parser.add_argument("--user", action="append", default=[], dest="users",
                        metavar="ID", help="only this user (default: every user)")
    parser.add_argument("--recursive", action="store_true",
                        help="read the videos below each item (a series' episodes) "
                             "rather than the item itself")
    _add_output(parser)
    _add_jobs(parser)
    parser.set_defaults(handler=_playstate)


def _expand_items(client: Any, ids: list[str], recursive: bool) -> list[str]:
    if not recursive:
        return ids
    out: list[str] = []
    for item_id in ids:
        out += [str(r["Id"]) for r in query.children(
            client, item_id, recursive=True, types=VIDEO_TYPES)]
    return out


#: What ``--recursive`` and ``--parent`` collect: the items that carry play state.
VIDEO_TYPES = ("Episode", "Movie", "Video", "MusicVideo")


def _playstate(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    ids = _expand_items(client, expand_lists(args.item_ids), args.recursive)
    report = query.playstate(client, ids, user_ids=args.users or None, workers=args.jobs)
    print(query.render(list(report.rows), query.PLAYSTATE_COLUMNS, args.format))
    for user, batch, error in report.errors:
        log.error("user %s, %d item(s): %s", user, len(batch), error)
    for item in report.missing:
        log.error("not found: %s", item)
    return 0 if report.ok else 1


REGISTRARS = {
    "find": _register_find,
    "children": _register_children,
    "playstate": _register_playstate,
}
