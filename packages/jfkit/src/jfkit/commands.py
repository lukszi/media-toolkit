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
the client: a dry-run client logs every write and sends none of them,
so a whole pipeline can be run against a real server and produce a complete
account of what it would do.

**An applied write to a server record keeps a rollback.** ``item set``,
``libopts set`` and ``segments scope`` each refuse ``--apply`` unless they are
told where to write the state they are about to replace (``--backup``,
``--backup-dir``, ``--backup-dir``), and they write it before anything is
sent. A rollback artefact the caller had to remember to ask for is not one.

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
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

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
from .jobs import RECENT_WINDOW_S, Gate, GateTimeout, observe, server_view, wait_until_clear
from .report import FORMATS
from .report import write as write_survey
from .safedelete import default_keeper_check, load_manifest, safe_delete
from .safedelete.junk import load_rules
from .service import ServiceControlError, ServiceController, controller_for

__all__ = [
    "REGISTRARS",
    "add_service_arguments",
    "add_write_arguments",
    "client_from",
    "expand_lists",
]

log = logging.getLogger(__name__)


def add_write_arguments(parser: argparse.ArgumentParser) -> None:
    """``--dry-run`` and ``--apply``, the only two states there are."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", dest="apply", action="store_false", default=False,
        help="say what would happen and change nothing (the default)",
    )
    group.add_argument("--apply", dest="apply", action="store_true", help="actually write")


#: Said in the help of every verb that takes many paths or identifiers.
LIST_HELP = (
    "; '-' reads more, one per line, from standard input, and @FILE from a "
    "file -- a long list does not fit on one command line"
)


def expand_lists(values: Sequence[str], *, stdin: TextIO | None = None) -> list[str]:
    """Arguments as given, with ``-`` and ``@FILE`` replaced by their lines.

    One entry per line, blank lines skipped, nothing else interpreted: a
    path with spaces is one line. The order is kept, so a caller can match
    each answer to its question by position as well as by the path printed
    beside it.
    """
    out: list[str] = []
    for value in values:
        if value == "-":
            source = stdin if stdin is not None else sys.stdin
            out += [line.rstrip("\r\n") for line in source if line.strip()]
        elif value.startswith("@") and len(value) > 1:
            text = Path(value[1:]).read_text(encoding="utf-8-sig")
            out += [line.rstrip("\r") for line in text.splitlines() if line.strip()]
        else:
            out.append(value)
    return out


def _refuse_without(args: argparse.Namespace, attribute: str, flag: str) -> bool:
    """Say so and return true when ``--apply`` came without its rollback flag."""
    if getattr(args, "apply", False) and getattr(args, attribute) is None:
        print(
            f"--apply writes the state it replaces first: name {flag}. "
            "Nothing was sent."
        )
        return True
    return False


def add_service_arguments(parser: argparse.ArgumentParser) -> None:
    """Which controller stops the service, and the service's registered name."""
    parser.add_argument("--service", default="manual",
                        choices=["manual", "windows", "systemd"])
    parser.add_argument(
        "--service-name", metavar="NAME",
        help="the service's registered name; required by every controller but "
             "the manual one",
    )
    parser.add_argument(
        "--service-program", metavar="PROGRAM",
        help="with --service windows: the control program, sc (the default) "
             "or the NSSM executable the service was installed with",
    )


def server_configured(config: Config) -> bool:
    """Whether the configuration describes a server a client could talk to.

    The address has a default, so its presence says nothing; a way to get a
    token is what only a configuration that means a server carries.
    """
    server = config.server
    return bool(server.url and (server.token_env or server.token_command))


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
                       help="where to write the record as it was, before changing "
                            "it; required with --apply")
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
        # stdout is the record and nothing else, so it can be piped into a
        # JSON reader whether or not a copy was saved
        print(f"written to {save(record, args.save)}", file=sys.stderr)
    print(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def _item_set(args: argparse.Namespace, config: Config) -> int:
    if _refuse_without(args, "backup", "--backup PATH"):
        return 2
    client = client_from(args, config)
    phases = update_item(
        client, args.item_id, _fields_from(args.field),
        refresh=lambda item_id: refresh_module.request_refresh(client, item_id),
        backup=args.backup,
    )
    for phase in phases:
        print(f"  {phase}")
        for change in phase.changes():
            print(f"      {change}")
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
    parser.add_argument("item_ids", nargs="+", metavar="ID",
                        help="the items to refresh" + LIST_HELP)
    parser.add_argument("--expect", action="append", default=[], metavar="FIELD",
                        help="a field this refresh is supposed to change; one that "
                             "did not change is reported and exits non-zero")
    parser.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS")
    parser.add_argument("--poll", type=float, default=10.0, metavar="SECONDS")
    add_write_arguments(parser)
    parser.set_defaults(handler=_refresh)


def _refresh(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    drifted = 0
    for item_id in expand_lists(args.item_ids):
        report = refresh_module.safe_refresh(
            client, item_id, expected_changes=args.expect,
            require_changes=args.expect,
            timeout_s=args.timeout, poll_s=args.poll,
        )
        print(report)
        drifted += not report.ok
    return 1 if drifted and not client.dry_run else 0


def _register_notify(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "notify", help="tell the server these paths changed, and nothing wider"
    )
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="the files or folders that changed" + LIST_HELP)
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="a library root, which is refused rather than notified")
    parser.add_argument("--kind", default="Modified",
                        choices=sorted(refresh_module.NOTIFY_KINDS))
    add_write_arguments(parser)
    parser.set_defaults(handler=_notify)


def _notify(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    # Every folder of every library the server has, not only the two the
    # configuration names: a root nobody wrote down starts the same full scan.
    served = refresh_module.library_roots(client)
    if served is None:
        print(
            "the server's library folders could not be read, so it cannot be "
            "shown that none of these paths is one. Nothing was sent."
        )
        return 2
    roots = list(args.root) + served + [
        str(p) for p in (config.paths.movies, config.paths.series) if p
    ]
    try:
        sent = refresh_module.notify_changed(
            client, [Path(p) for p in expand_lists(args.paths)], roots=roots,
            kind=args.kind,
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
    write.add_argument("--backup-dir", type=Path, metavar="DIR",
                       help="where to copy the document before the write; "
                            "required with --apply")
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
    if _refuse_without(args, "backup_dir", "--backup-dir DIR"):
        return 2
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
    add_service_arguments(run)
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
    if args.apply and args.snapshot_dir is None:
        print("--apply copies the database first: name --snapshot-dir DIR")
        return 2
    # The server is asked whether it is busy when one is configured, and
    # not otherwise: the pass itself needs nothing but the file.
    client = client_from(args, config) if server_configured(config) else None
    try:
        controller = _controller(args)
    except ServiceControlError as refused:
        print(str(refused))
        return 2
    report = maintenance_module.run_operations(
        args.database, operations,
        controller=controller,
        snapshot_dir=args.snapshot_dir,
        dry_run=not args.apply,
        client=client,
    )
    print(report)
    return 0 if report.ok else 1


def _controller(args: argparse.Namespace) -> ServiceController:
    return controller_for(
        args.service, name=args.service_name, program=args.service_program
    )


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
                        help="tab-separated: item_id, live path, replacement path, "
                        "and optionally the stream count the record should show")
    parser.add_argument("--parked", type=Path, required=True)
    parser.add_argument("--chunk-gib", type=float,
                        default=swap_module.DEFAULT_CHUNK_GIB)
    parser.add_argument("--budget", type=float, metavar="SECONDS",
                        help="the longest outage worth taking in one chunk")
    parser.add_argument("--rate", type=float, metavar="MIB_PER_SECOND",
                        help="observed copy rate, so the budget means something")
    parser.add_argument("--user", action="append", default=[], metavar="ID",
                        help="a user whose play state is snapshotted and replayed "
                        "(default: every user the server lists)")
    parser.add_argument(
        "--replacement-check", choices=["full", "quick"], default="full",
        help="how each replacement is proved to play before anything is parked: "
             "full (sampled, listed and decoded; the default) or quick (not "
             "decoded). There is no way to skip it",
    )
    add_service_arguments(parser)
    add_write_arguments(parser)
    parser.set_defaults(handler=_swap)


def _swap(args: argparse.Namespace, config: Config) -> int:
    try:
        controller = _controller(args)
    except ServiceControlError as refused:
        print(str(refused))
        return 2
    pairs = []
    for line in args.plan.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        item_id, keeper, replacement = fields[:3]
        streams = int(fields[3]) if len(fields) > 3 and fields[3].strip() else None
        pairs.append(
            swap_module.Pair(item_id, Path(keeper), Path(replacement), streams=streams)
        )
    report = swap_module.swap(
        client_from(args, config), pairs,
        controller=controller,
        parked=args.parked, chunk_gib=args.chunk_gib,
        users=args.user, mib_per_second=args.rate, budget_s=args.budget,
        replacement_check=swap_module.default_replacement_check(
            decode=args.replacement_check == "full"
        ),
    )
    print(report)
    return 0 if report.ok else 1


# ------------------------------------------------------------------ delete
def _register_delete(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "delete", help="park what a named category released, after checking each one",
        epilog="The leftover categories (release-junk, dead-release-folder, "
               "corrupt-unplayable, sample) take an empty item_id; "
               "'jfkit leftovers sweep' proposes them from a walk of the library.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--release", action="append", default=[], metavar="CATEGORY",
                        help="a category somebody decided is deletable")
    parser.add_argument("--parked", type=Path, required=True)
    parser.add_argument("--backup-folders", type=Path, metavar="DIR")
    parser.add_argument("--user", action="append", default=[], metavar="ID",
                        help="a user whose play state is checked "
                        "(default: every user the server lists)")
    parser.add_argument("--audit", type=Path, metavar="PATH")
    parser.add_argument(
        "--keeper-check", choices=["full", "quick"], default="full",
        help="how the kept copy's payload is proved before a copy is removed: "
             "full (sampled, listed and decoded; the default) or quick (not "
             "decoded). There is no way to skip it",
    )
    parser.add_argument(
        "--integrity", choices=["full", "quick"], default="full",
        help="how a corrupt-unplayable candidate is measured: full (sampled, "
             "listed and decoded; the default) or quick (not decoded)",
    )
    parser.add_argument("--rules", type=Path, metavar="FILE",
                        help="a TOML file whose [leftovers] table widens or replaces "
                             "the junk rules")
    add_write_arguments(parser)
    parser.set_defaults(handler=_delete)


def _delete(args: argparse.Namespace, config: Config) -> int:
    report = safe_delete(
        client_from(args, config), load_manifest(args.manifest),
        allowed_categories=args.release, parked=args.parked,
        users=args.user, audit=args.audit, backup_folders=args.backup_folders,
        keeper_check=default_keeper_check(decode=args.keeper_check == "full"),
        rules=load_rules(args.rules),
        integrity_check=default_keeper_check(decode=args.integrity == "full"),
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
    scope.add_argument("--backup-dir", type=Path, metavar="DIR",
                       help="where to write the plugin's configuration as it was, "
                            "before changing it; required with --apply")
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
    if _refuse_without(args, "backup_dir", "--backup-dir DIR"):
        return 2
    client = client_from(args, config)
    plugin = segments_module.find_plugin(client, args.plugin)
    items = surveys_module.fetch_items(client, types=("Movie", "Episode"))
    covered = [
        item["Id"] for item in items
        if segments_module.segments_for(client, str(item["Id"]))
    ]
    found = segments_module.coverage(items, covered, fraction=args.fraction)
    print(found)
    print(segments_module.scope_plugin(
        client, plugin, found, backup_dir=args.backup_dir
    ))
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
                       help="do not ask the server what it is running or playing")
    check.add_argument("--sample", type=float, default=1.0, metavar="S",
                       help="watch the device's own counters for this long; 0 skips it "
                            "(default: %(default)s)")
    check.add_argument("--recent-window", type=float, default=RECENT_WINDOW_S / 60,
                       metavar="MIN",
                       help="items the server changed within this many minutes count as "
                            "work it may still be doing; 0 skips it (default: %(default)s)")
    check.add_argument("--lock", action="append", default=[], type=Path, metavar="PATH",
                       help="a lock file whose existence holds the gate; repeatable")
    check.add_argument("--wait", type=float, default=0.0, metavar="MIN",
                       help="look again every minute until clear, for at most this long")
    check.add_argument("--json", action="store_true", help="print the gate as JSON")
    check.set_defaults(handler=_jobs_gate)

    lanes = verbs.add_parser("lanes", help="group work by device, largest first")
    lanes.add_argument("paths", nargs="+", type=Path)
    lanes.add_argument("--per-device", type=int, default=1, metavar="N")
    lanes.set_defaults(handler=_jobs_lanes)


def _jobs_gate(args: argparse.Namespace, config: Config) -> int:
    """Exit 0 when clear, 1 when RED; the output names every signal that held it."""
    device = device_of(args.path)
    window_s = max(0.0, args.recent_window * 60)
    client = (
        client_from(args, config)
        if not args.ignore_server and server_configured(config) else None
    )

    def look() -> Gate:
        view = (
            server_view(client, recent_window_s=window_s) if client is not None else None
        )
        return observe(
            device, view=view, own_tag=args.tag, sample_s=args.sample,
            locks=args.lock, recent_window_s=window_s,
            max_readers=config.jobs.max_readers_per_device,
        )

    try:
        found = wait_until_clear(
            look, poll_s=60.0, timeout_s=max(0.0, args.wait * 60),
            on_hold=lambda held: log.info("%s", held),
        )
    except GateTimeout as exc:
        found = exc.gate
    if args.json:
        print(json.dumps(found.as_dict(), ensure_ascii=False, indent=2))
    else:
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
