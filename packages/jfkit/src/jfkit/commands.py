"""jfkit.commands -- the server-side sub-commands.

Ten verbs, in the order a job uses them: look at an item, predict how its
filename will be read, survey what is there, change a field, ask for a
refresh, tell the server one path changed, adjust a library's options, put a
rebuild in an item's place, remove what somebody released, keep the database,
and scope a segment pass.

The two properties are the file-side entry point's, unchanged.

**Reading is free; writing is asked for twice.** Every verb that changes
anything takes ``--dry-run``, which is the default, and ``--apply``. There is
no third state and no environment variable that flips it. The switch builds
the client: a dry-run client logs every write in full and sends none of them,
so a whole pipeline can be run against a real server and produce a complete
account of what it would do.

**The exit code carries the answer.** A refresh that drifted, a swap whose
record does not match, a deletion whose preconditions failed and a
maintenance pass whose row counts moved all exit non-zero, because these
belong in pipelines where "it printed something" is not a signal.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from . import libopts as libopts_module
from . import maintenance as maintenance_module
from . import refresh as refresh_module
from . import segments as segments_module
from . import surveys as surveys_module
from . import swap as swap_module
from .client import Client
from .config import Config
from .devices import device_of
from .dto import compare, fetch, load, save, update_item
from .jobs import gate as device_gate
from .report import FORMATS
from .report import write as write_survey
from .safedelete import load_manifest, safe_delete
from .service import controller_for

__all__ = ["REGISTRARS", "add_write_arguments", "client_from"]

log = logging.getLogger(__name__)


def add_write_arguments(parser: argparse.ArgumentParser) -> None:
    """``--dry-run`` and ``--apply``, the only two states there are."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="apply", action="store_false", default=False,
        help="say what would happen and change nothing (the default)",
    )
    group.add_argument("--apply", dest="apply", action="store_true", help="actually write")


def client_from(args: argparse.Namespace, config: Config) -> Client:
    """One client, built from the one switch that decides whether it writes."""
    return Client(config, dry_run=not getattr(args, "apply", False))


def _fields_from(pairs: list[str]) -> dict[str, Any]:
    """``Name=Blue Canyon`` and ``IndexNumber=3`` into a body.

    A value that parses as JSON is used as JSON, so a number stays a number
    and a null is a null. Anything else is a string, which is what a title is.
    """
    out: dict[str, Any] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        if not key or not _:
            raise SystemExit(f"--field takes KEY=VALUE, not {pair!r}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


# -------------------------------------------------------------------- item
def _register_item(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("item", help="read, write and compare one record")
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    show = verbs.add_parser("show", help="the whole record, as the server has it")
    show.add_argument("item_id")
    show.add_argument("--save", type=Path, metavar="PATH",
                      help="write it here as well, as a rollback artefact")
    show.set_defaults(handler=_item_show)

    write = verbs.add_parser("set", help="change named fields, in phase order")
    write.add_argument("item_id")
    write.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
    write.add_argument("--backup", type=Path, metavar="PATH",
                       help="where to write the record as it was, before changing it")
    add_write_arguments(write)
    write.set_defaults(handler=_item_set)

    diff = verbs.add_parser("diff", help="compare the record with a saved one")
    diff.add_argument("item_id")
    diff.add_argument("snapshot", type=Path)
    diff.add_argument("--expect", action="append", default=[], metavar="FIELD")
    diff.set_defaults(handler=_item_diff)


def _item_show(args: argparse.Namespace, config: Config) -> int:
    record = fetch(client_from(args, config), args.item_id)
    if args.save:
        print(f"written to {save(record, args.save)}")
    print(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def _item_set(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    phases = update_item(
        client, args.item_id, _fields_from(args.field),
        refresh=lambda item_id: refresh_module.request_refresh(client, item_id),
        backup=args.backup,
    )
    for phase in phases:
        print(f"  {phase}")
    print("nothing was sent: this was a dry run" if client.dry_run else "applied")
    return 0


def _item_diff(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    found = compare(load(args.snapshot), fetch(client, args.item_id),
                    expected=args.expect)
    print(found)
    return 0 if found.ok else 1


# ----------------------------------------------------------------- refresh
def _register_refresh(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "refresh", help="refresh without replacing, wait for it, and diff the result"
    )
    parser.add_argument("item_ids", nargs="+", metavar="ID")
    parser.add_argument("--expect", action="append", default=[], metavar="FIELD",
                        help="a field this refresh is supposed to change")
    parser.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
    parser.add_argument("--poll", type=float, default=10.0, metavar="SECONDS")
    add_write_arguments(parser)
    parser.set_defaults(handler=_refresh)


def _refresh(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    drifted = 0
    for item_id in args.item_ids:
        report = refresh_module.safe_refresh(
            client, item_id, expected_changes=args.expect,
            timeout_s=args.timeout, poll_s=args.poll,
        )
        print(report)
        drifted += not report.ok
    return 1 if drifted and not client.dry_run else 0


def _register_notify(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "notify", help="tell the server these paths changed, and nothing wider"
    )
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH")
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="a library root, which is refused rather than notified")
    parser.add_argument("--kind", default="Modified",
                        choices=sorted(refresh_module.NOTIFY_KINDS))
    add_write_arguments(parser)
    parser.set_defaults(handler=_notify)


def _notify(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    roots = list(args.root) + [
        str(p) for p in (config.paths.movies, config.paths.series) if p
    ]
    try:
        sent = refresh_module.notify_changed(
            client, args.paths, roots=roots, kind=args.kind
        )
    except refresh_module.NotifyRefused as refused:
        print(str(refused))
        return 2
    for path in sent:
        print(f"  {args.kind}: {path}")
    return 0


# ----------------------------------------------------------------- surveys
def _register_survey(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "survey", help="read-only inventories: completeness, languages, chapters, more"
    )
    parser.add_argument("name", choices=surveys_module.names())
    parser.add_argument("--out", type=Path, metavar="DIR",
                        help="write every format here instead of printing one")
    parser.add_argument("--format", default="md", choices=sorted(FORMATS))
    parser.add_argument("--type", action="append", default=[], metavar="TYPE")
    parser.set_defaults(handler=_survey)


def _survey(args: argparse.Namespace, config: Config) -> int:
    from .report import render

    client = client_from(args, config)
    items = (
        surveys_module.fetch_items(client, types=args.type) if args.type
        else surveys_module.fetch_items(client)
    )
    survey = surveys_module.build(args.name, items)
    if args.out:
        for path in write_survey(survey, args.out):
            print(f"  {path}")
        return 0
    print(render(survey, args.format))
    return 0


# ----------------------------------------------------------------- libopts
def _register_libopts(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "libopts", help="a library's options, including the ones the file omits"
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    show = verbs.add_parser("show", help="the effective options, defaults filled in")
    show.add_argument("document", type=Path, help="the library's options document")
    show.set_defaults(handler=_libopts_show)

    check = verbs.add_parser(
        "roots", help="libraries whose stored path is not under the data directory"
    )
    check.add_argument("--data-dir", type=Path, required=True)
    check.set_defaults(handler=_libopts_roots)

    write = verbs.add_parser("set", help="change one option, and verify what moved")
    write.add_argument("document", type=Path)
    write.add_argument("--id", required=True, dest="library_id")
    write.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
    write.add_argument("--backup-dir", type=Path, metavar="DIR")
    add_write_arguments(write)
    write.set_defaults(handler=_libopts_set)


def _libopts_show(args: argparse.Namespace, _config: Config) -> int:
    options = libopts_module.read_options(args.document)
    print(options)
    for key in sorted(options.values):
        mark = "  (default)" if key in options.defaulted else ""
        print(f"  {key} = {options.values[key]!r}{mark}")
    return 0


def _libopts_roots(args: argparse.Namespace, config: Config) -> int:
    problems = libopts_module.root_path_problems(
        client_from(args, config), data_dir=args.data_dir
    )
    for problem in problems:
        print(f"  {problem}")
    print(
        f"{len(problems)} librarie(s) the server cannot match to their records"
        if problems else "every library resolves to its record"
    )
    return 1 if problems else 0


def _libopts_set(args: argparse.Namespace, config: Config) -> int:
    options = libopts_module.read_options(args.document, library_id=args.library_id)
    result = libopts_module.write_options(
        client_from(args, config), options, _fields_from(args.field),
        backup_dir=args.backup_dir,
    )
    print(result)
    return 0 if result.ok else 1


# -------------------------------------------------------------- maintenance
def _register_maintenance(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "maintenance", help="database and preview upkeep, with the guards kept"
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    copy = verbs.add_parser("snapshot", help="a safe copy of a live database")
    copy.add_argument("database", type=Path)
    copy.add_argument("out", type=Path)
    copy.set_defaults(handler=_maintenance_snapshot)

    check = verbs.add_parser("check", help="what the database says about itself")
    check.add_argument("database", type=Path)
    check.set_defaults(handler=_maintenance_check)

    run = verbs.add_parser("run", help="named fixes, with the service stopped")
    run.add_argument("database", type=Path)
    run.add_argument("--reindex", action="store_true", help="rebuild every index")
    run.add_argument("--repoint", nargs=2, metavar=("OLD", "NEW"),
                     help="rewrite stored paths after the data directory moved")
    run.add_argument("--expect-rows", type=int, metavar="N")
    run.add_argument("--snapshot-dir", type=Path, metavar="DIR")
    run.add_argument("--service", default="manual",
                     choices=["manual", "windows", "systemd"])
    add_write_arguments(run)
    run.set_defaults(handler=_maintenance_run)

    previews = verbs.add_parser(
        "previews", help="put back preview tiles that are missing, and nothing else"
    )
    previews.add_argument("backup", type=Path)
    previews.add_argument("live", type=Path)
    add_write_arguments(previews)
    previews.set_defaults(handler=_maintenance_previews)


def _maintenance_snapshot(args: argparse.Namespace, _config: Config) -> int:
    print(maintenance_module.snapshot(args.database, args.out))
    return 0


def _maintenance_check(args: argparse.Namespace, _config: Config) -> int:
    answer = maintenance_module.integrity(args.database)
    for line in answer:
        print(f"  {line}")
    for table, count in maintenance_module.counts(args.database).items():
        print(f"  {table}: {count} row(s)")
    return 0 if answer == ["ok"] else 1


def _maintenance_run(args: argparse.Namespace, config: Config) -> int:
    operations: list[maintenance_module.Operation] = []
    if args.repoint:
        operations.append(maintenance_module.RepointPaths(
            args.repoint[0], args.repoint[1], expect_rows=args.expect_rows
        ))
    if args.reindex:
        operations.append(maintenance_module.Reindex())
    if not operations:
        print("nothing to do: name --repoint or --reindex")
        return 2
    client = client_from(args, config)
    report = maintenance_module.run_operations(
        args.database, operations,
        controller=controller_for(args.service),
        snapshot_dir=args.snapshot_dir,
        dry_run=not args.apply,
        client=client if config.server.url else None,
    )
    print(report)
    return 0 if report.ok else 1


def _maintenance_previews(args: argparse.Namespace, _config: Config) -> int:
    report = maintenance_module.restore_previews(
        args.backup, args.live, dry_run=not args.apply
    )
    print(report)
    return 0


# -------------------------------------------------------------------- swap
def _register_swap(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "swap", help="put rebuilt files in their items' places, in chunks"
    )
    parser.add_argument("plan", type=Path,
                        help="tab-separated: item_id, live path, replacement path")
    parser.add_argument("--parked", type=Path, required=True)
    parser.add_argument("--chunk-gib", type=float,
                        default=swap_module.DEFAULT_CHUNK_GIB)
    parser.add_argument("--budget", type=float, metavar="SECONDS",
                        help="the longest outage worth taking in one chunk")
    parser.add_argument("--rate", type=float, metavar="MIB_PER_SECOND",
                        help="observed copy rate, so the budget means something")
    parser.add_argument("--user", action="append", default=[], metavar="ID",
                        help="a user whose play state is snapshotted and replayed")
    parser.add_argument("--service", default="manual",
                        choices=["manual", "windows", "systemd"])
    add_write_arguments(parser)
    parser.set_defaults(handler=_swap)


def _swap(args: argparse.Namespace, config: Config) -> int:
    pairs = []
    for line in args.plan.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item_id, keeper, replacement = line.split("\t")[:3]
        pairs.append(swap_module.Pair(item_id, Path(keeper), Path(replacement)))
    report = swap_module.swap(
        client_from(args, config), pairs,
        controller=controller_for(args.service),
        parked=args.parked, chunk_gib=args.chunk_gib,
        users=args.user, mib_per_second=args.rate, budget_s=args.budget,
    )
    print(report)
    return 0 if report.ok else 1


# ------------------------------------------------------------------ delete
def _register_delete(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "delete", help="park what a named category released, after checking each one"
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--release", action="append", default=[], metavar="CATEGORY",
                        help="a category somebody decided is deletable")
    parser.add_argument("--parked", type=Path, required=True)
    parser.add_argument("--backup-folders", type=Path, metavar="DIR")
    parser.add_argument("--user", action="append", default=[], metavar="ID")
    parser.add_argument("--audit", type=Path, metavar="PATH")
    add_write_arguments(parser)
    parser.set_defaults(handler=_delete)


def _delete(args: argparse.Namespace, config: Config) -> int:
    report = safe_delete(
        client_from(args, config), load_manifest(args.manifest),
        allowed_categories=args.release, parked=args.parked,
        users=args.user, audit=args.audit, backup_folders=args.backup_folders,
    )
    print(report)
    return 1 if report.refused else 0


# ---------------------------------------------------------------- segments
def _register_segments(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "segments", help="scope a segment pass to media that is not covered yet"
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    listing = verbs.add_parser("tasks", help="what the server is running right now")
    listing.set_defaults(handler=_segments_tasks)

    scope = verbs.add_parser("scope", help="exclude what is already covered")
    scope.add_argument("--plugin", required=True, metavar="NAME",
                       help="part of the plugin's name; never an identifier")
    scope.add_argument("--fraction", type=float,
                       default=segments_module.COVERED_FRACTION)
    add_write_arguments(scope)
    scope.set_defaults(handler=_segments_scope)

    stop = verbs.add_parser("cancel", help="stop a running task")
    stop.add_argument("--task", required=True, metavar="NAME")
    add_write_arguments(stop)
    stop.set_defaults(handler=_segments_cancel)


def _segments_tasks(args: argparse.Namespace, config: Config) -> int:
    for task in segments_module.tasks(client_from(args, config)):
        mark = " <- running" if task.running else ""
        print(f"  {task.name}: {task.state}{mark}")
    return 0


def _segments_scope(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    plugin = segments_module.find_plugin(client, args.plugin)
    items = surveys_module.fetch_items(client, types=("Movie", "Episode"))
    covered = [
        item["Id"] for item in items
        if segments_module.segments_for(client, str(item["Id"]))
    ]
    found = segments_module.coverage(items, covered, fraction=args.fraction)
    print(found)
    print(segments_module.scope_plugin(client, plugin, found))
    return 0


def _segments_cancel(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    task = segments_module.find_task(client, args.task)
    if not task.running:
        print(f"{task.name} is {task.state or 'not running'}; nothing to stop")
        return 0
    segments_module.cancel_task(client, task)
    print(f"asked {task.name} to stop")
    return 0


# -------------------------------------------------------------------- jobs
def _register_jobs(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "jobs", help="which device backs a path, and whether it is busy"
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    check = verbs.add_parser("gate", help="may a heavy reader start on this path")
    check.add_argument("path", type=Path)
    check.add_argument("--tag", metavar="STRING",
                       help="a marker this job's own workers carry, so it is not "
                            "counted as somebody else's reader")
    check.add_argument("--ignore-server", action="store_true",
                       help="do not ask the server what it is running")
    check.set_defaults(handler=_jobs_gate)

    lanes = verbs.add_parser("lanes", help="group work by device, largest first")
    lanes.add_argument("paths", nargs="+", type=Path)
    lanes.add_argument("--per-device", type=int, default=1, metavar="N")
    lanes.set_defaults(handler=_jobs_lanes)


def _jobs_gate(args: argparse.Namespace, config: Config) -> int:
    running: list[str] = []
    if not args.ignore_server and config.server.url:
        running = [task.name for task in
                   segments_module.running(client_from(args, config))]
    found = device_gate(device_of(args.path), running_tasks=running, own_tag=args.tag)
    print(found)
    if found.rotational is True:
        print("  this device spins: one sequential reader at a time")
    return 0 if found.open else 1


def _jobs_lanes(args: argparse.Namespace, _config: Config) -> int:
    from .jobs import pack_lanes

    for lane in pack_lanes(
        args.paths, path_of=lambda p: p, lanes=args.per_device
    ):
        print(f"  {lane}")
        for item in lane.items:
            print(f"      {item}")
    return 0


REGISTRARS = {
    "item": _register_item,
    "refresh": _register_refresh,
    "notify": _register_notify,
    "survey": _register_survey,
    "libopts": _register_libopts,
    "maintenance": _register_maintenance,
    "swap": _register_swap,
    "delete": _register_delete,
    "segments": _register_segments,
    "jobs": _register_jobs,
}
