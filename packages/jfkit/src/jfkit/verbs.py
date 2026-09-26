"""jfkit.verbs -- the read verbs, and watched state carried across a rename.

``find``, ``children`` and ``playstate`` only read. They print rows as
tab-separated text (the default, with a header line) or as JSON, so they pipe
into whatever reads them next -- a mapping for a replay, a list for
``refresh``. ``userdata`` snapshots every user's watched state, replays it
onto new identifiers through a mapping and verifies it; its replay is a plan
(printed by the dry run, saved with ``--plan-out``) applied with an audit and
resumable after a partial failure.

``playstate`` and every ``userdata`` verb take ``--jobs N``: how many requests
run at once, default :data:`mkvkit.lanes.DEFAULT_WORKERS`.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from mkvkit.lanes import DEFAULT_WORKERS
from mkvkit.steps import add_plan_arguments, apply, load_plan

from . import query
from . import userdata as userdata_module
from .commands import LIST_HELP, add_write_arguments, client_from, expand_lists
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


# -------------------------------------------------------------- userdata
def _register_userdata(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "userdata", help="snapshot every user's watched state, replay it after a rename",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    snap = verbs.add_parser("snapshot", help="write every user's state for items to a file")
    snap.add_argument("item_ids", nargs="*", metavar="ID", help="item ids" + LIST_HELP)
    snap.add_argument("--parent", action="append", default=[], metavar="ID",
                      help="every video below this item (a series, a season)")
    snap.add_argument("--out", type=Path, required=True, metavar="PATH")
    snap.add_argument("--user", action="append", default=[], dest="users", metavar="ID")
    _add_jobs(snap)
    snap.set_defaults(handler=_userdata_snapshot)

    def scoped(verb: argparse.ArgumentParser) -> None:
        verb.add_argument("snapshot", type=Path)
        verb.add_argument("--map", type=Path, dest="mapping", metavar="PATH",
                          help="old id -> new id, as a JSON object or two TSV columns; "
                               "an id not in it kept its identity")
        verb.add_argument(
            "--scope-parent", action="append", default=[], metavar="ID",
            help="also check every video below this item (the whole series), and "
                 "clear state the snapshot does not account for",
        )
        verb.add_argument("--scope", action="append", default=[], metavar="ID",
                          help="also check this item" + LIST_HELP)
        _add_jobs(verb)

    replay = verbs.add_parser(
        "replay", help="write the snapshot onto the new ids, clear inherited state",
        epilog="exit status: 0 when nothing is left to do or everything was applied "
               "and verified, 1 when a step failed or a row still differs, 2 for a "
               "usage error.",
    )
    scoped(replay)
    add_plan_arguments(replay)
    add_write_arguments(replay)
    replay.set_defaults(handler=_userdata_replay)

    check = verbs.add_parser(
        "verify", help="compare the server with what the snapshot justifies",
        epilog="exit status: 0 when every row matches (a kept last-played date is "
               "reported, not failed), 1 otherwise.",
    )
    scoped(check)
    check.set_defaults(handler=_userdata_verify)


def _userdata_snapshot(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    ids = expand_lists(args.item_ids)
    for parent in args.parent:
        ids += [str(r["Id"]) for r in query.children(
            client, parent, recursive=True, types=VIDEO_TYPES)]
    if not ids:
        print("nothing to snapshot: name item ids or --parent")
        return 2
    taken = userdata_module.snapshot(client, ids, user_ids=args.users or None,
                                     workers=args.jobs)
    path = taken.save(args.out)
    rows = sum(1 for item in taken.items for s in item.states.values() if not s.blank)
    print(f"{len(taken.items)} item(s), {len(taken.users)} user(s), {rows} row(s) with "
          f"state: written to {path}")
    return 0


def _scope(args: argparse.Namespace, client: Any) -> list[str]:
    out = expand_lists(args.scope)
    for parent in args.scope_parent:
        out += [str(r["Id"]) for r in query.children(
            client, parent, recursive=True, types=VIDEO_TYPES)]
    return out


def _userdata_replay(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    taken = userdata_module.Snapshot.load(args.snapshot)
    mapping = userdata_module.load_mapping(args.mapping) if args.mapping else {}
    scope = _scope(args, client)
    if args.plan_in is not None:
        plan = load_plan(args.plan_in)
    else:
        plan = userdata_module.plan_replay(client, taken, mapping, scope=scope,
                                           workers=args.jobs)
    print(plan.render())
    if args.plan_out is not None:
        print(f"plan written to {plan.save(args.plan_out)}")
    if not client.dry_run and args.audit is None:
        print("--apply records every write: name --audit PATH. Nothing was sent.")
        return 2
    if client.dry_run:
        print("nothing was sent: this was a dry run")
        return 0
    report = apply(plan, userdata_module.actions(client), audit=args.audit)
    print(report)
    if not report.ok:
        return 1
    checked = userdata_module.verify(client, taken, mapping, scope=scope, workers=args.jobs)
    print(checked)
    return 0 if checked.ok else 1


def _userdata_verify(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    taken = userdata_module.Snapshot.load(args.snapshot)
    mapping = userdata_module.load_mapping(args.mapping) if args.mapping else {}
    checked = userdata_module.verify(client, taken, mapping, scope=_scope(args, client),
                                     workers=args.jobs)
    print(checked)
    return 0 if checked.ok else 1


def _register_dedupe(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    from .dedupe.cli import register

    register(subparsers)


def _register_rename(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    from .rename.verb import register

    register(subparsers)


REGISTRARS = {
    "dedupe": _register_dedupe,
    "find": _register_find,
    "children": _register_children,
    "playstate": _register_playstate,
    "userdata": _register_userdata,
    "rename": _register_rename,
}
