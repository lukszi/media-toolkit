"""``jfkit dedupe``: find copies, pick keepers, and park the rest as one audited plan.

The dry run is the default and it does all the reading: the catalogue, every
copy's headers, every keeper's payload and every user's watched state. It
prints one verdict per group and the plan, and changes nothing. ``--apply``
runs the plan with a JSON-lines audit; the same plan and the same audit
again resume a run that stopped.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mkvkit.lanes import DEFAULT_WORKERS
from mkvkit.steps import (
    Plan,
    add_plan_arguments,
    apply,
    describe_status,
    load_plan,
    read_audit,
    status,
)

from ..client import Client
from ..commands import add_write_arguments, client_from
from ..config import Config
from ..healthlink import lane_gate
from ..surveys import fetch_items
from .groups import GroupScan, find_groups
from .plan import actions, build_plan
from .resolver import BeforeEach, Verdict, default_checker, resolve
from .rules import BLOCKED, VERDICTS, Rules

__all__ = ["register", "render_table", "render_tsv"]

log = logging.getLogger(__name__)

#: The catalogue fields grouping needs; asked for once, in one query.
FIELDS = (
    "Path", "ProviderIds", "MediaSources", "SeriesId", "SeriesName", "IndexNumber",
    "ParentIndexNumber", "IndexNumberEnd", "RunTimeTicks", "ProductionYear",
)

#: Where losers are parked when neither ``--parked`` nor ``[paths] parked``
#: says: relative, so on each file's own volume.
DEFAULT_PARKED = Path("_parked")

TYPES = {"movie": "Movie", "episode": "Episode"}


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "dedupe",
        help="resolve copies of one film or episode into a keeper and parked copies",
        description="Group copies by provider identifier (films) and by series, "
                    "season and episode (episodes), probe every copy, pick a keeper "
                    "by the rules in [policy] and [policy.dedupe], prove the keeper "
                    "plays, and plan: carry watched state, park the other copies "
                    "with their sidecars, park emptied release folders, notify.",
        epilog="exit status: 0 when every group was decided and, with --apply, every "
               "step ran; 1 when a group is BLOCKED or a step failed; 2 for a usage "
               "error.",
    )
    parser.add_argument("--type", action="append", default=[], dest="types",
                        choices=sorted(TYPES), help="only films or only episodes "
                        "(default: both)")
    parser.add_argument("--parent", action="append", default=[], metavar="ID",
                        help="only below this library, series or folder")
    parser.add_argument(
        "--keeper-check", choices=["full", "quick"],
        help="how the keeper's payload is read while planning: full decodes it, "
             "quick lists every packet (default: policy.dedupe.keeper_check). There "
             "is no way to skip it",
    )
    parser.add_argument("--parked", type=Path, metavar="DIR",
                        help="where parked copies go, keeping their layout; a relative "
                             "folder is taken on each file's own volume (default: "
                             "[paths] parked, else _parked on each volume)")
    parser.add_argument("--json", type=Path, metavar="PATH",
                        help="also write every verdict, copy and the plan as JSON")
    parser.add_argument("--tsv", type=Path, metavar="PATH",
                        help="also write one row per copy as tab-separated text")
    parser.add_argument("--jobs", type=int, default=DEFAULT_WORKERS, metavar="N",
                        help=f"server requests at once (default {DEFAULT_WORKERS}); "
                             "disks are always read by one reader each")
    parser.add_argument("--no-gate", action="store_true",
                        help="do not wait for other readers of a disk before reading it")
    parser.add_argument("--gate-wait", type=float, default=300.0, metavar="SECONDS",
                        help="how long to wait for a busy disk before giving up on a "
                             "copy (default 300)")
    add_plan_arguments(parser)
    add_write_arguments(parser)
    parser.set_defaults(handler=_dedupe)


# ------------------------------------------------------------------ the gate
def _gate(args: argparse.Namespace, config: Config) -> BeforeEach | None:
    """The device gate before every read: the one ``mkvkit health`` waits on.

    It holds a disk that somebody plays from, a hidden or named reader, a
    running server task, recent library changes or a lock file, looks again
    before every copy (a probe and a payload read are whole-file work), and
    gives the copy up after ``--gate-wait`` seconds of RED.
    """
    if args.no_gate or not config.jobs.device_gate:
        return None
    return lane_gate(config, every_s=0.0, timeout_s=max(0.0, args.gate_wait))


# ----------------------------------------------------------------- rendering
def _counts(verdicts: Sequence[Verdict]) -> dict[str, int]:
    return {name: sum(1 for v in verdicts if v.verdict == name) for name in VERDICTS}


def render_table(verdicts: Sequence[Verdict], scan: GroupScan | None = None) -> str:
    """The verdict table: one block per group, reasons included."""
    counts = _counts(verdicts)
    lines = [
        f"{len(verdicts)} group(s): "
        + ", ".join(f"{counts[name]} {name}" for name in VERDICTS)
    ]
    if scan is not None:
        lines += [f"  {note}" for note in scan.notes()]
    for number, verdict in enumerate(verdicts, start=1):
        cls = f" [{verdict.group.cls}]" if verdict.group.cls else ""
        lines.append(
            f"[{number}] {verdict.verdict:<13} {verdict.group.kind:<7} "
            f"{verdict.group.key}  {verdict.title}{cls}"
        )
        shown = {c.member.item_id + c.member.path for c in verdict.copies}
        if verdict.keeper is not None:
            lines.append(f"      keep  {verdict.keeper.member.path}  "
                         f"({verdict.keeper.summary()})")
        for loser in verdict.losers:
            lines.append(f"      park  {loser.member.path}  ({loser.summary()})")
        if verdict.keeper is None:
            for copy in verdict.copies:
                lines.append(f"      copy  {copy.member.path}  ({copy.summary()})")
            for member in verdict.group.members:
                if member.item_id + member.path not in shown:
                    lines.append(f"      row   {member.path}")
        lines += [f"      - {reason}" for reason in verdict.reasons]
        lines += [f"      note: {note}" for note in verdict.notes]
    return "\n".join(lines)


TSV_COLUMNS = ("group", "verdict", "kind", "class", "key", "title", "role", "item_id",
               "path", "copy", "reasons")


def _rows(verdicts: Sequence[Verdict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, verdict in enumerate(verdicts, start=1):
        copies = {c.member.item_id + c.member.path: c for c in verdict.copies}
        keeper = verdict.keeper.member.path if verdict.keeper is not None else None
        losers = {c.member.path for c in verdict.losers}
        for member in verdict.group.members:
            copy = copies.get(member.item_id + member.path)
            role = ("keep" if member.path == keeper else
                    "park" if member.path in losers else "member")
            rows.append({
                "group": number, "verdict": verdict.verdict, "kind": verdict.group.kind,
                "class": verdict.group.cls or "copies", "key": verdict.group.key,
                "title": verdict.title, "role": role, "item_id": member.item_id,
                "path": member.path, "copy": copy.summary() if copy else "",
                "reasons": " | ".join(verdict.reasons),
            })
    return rows


def render_tsv(verdicts: Sequence[Verdict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=TSV_COLUMNS, delimiter="\t",
                            lineterminator="\n")
    writer.writeheader()
    for row in _rows(verdicts):
        writer.writerow({k: str(v).replace("\t", " ").replace("\n", " ")
                         for k, v in row.items()})
    return out.getvalue()


def _document(
    verdicts: Sequence[Verdict], rules: Rules, plan: Plan | None, scan: GroupScan
) -> dict[str, Any]:
    return {
        "summary": _counts(verdicts),
        "notes": scan.notes(),
        "policy": rules.as_dict(),
        "groups": [v.as_dict() for v in verdicts],
        "plan": None if plan is None else plan.to_json(),
    }


# ------------------------------------------------------------------- the verb
def _apply_saved(args: argparse.Namespace, config: Config, rules: Rules) -> int:
    plan = load_plan(args.plan_in)
    if not args.apply:
        print(plan.render())
        if args.audit is not None:
            for line in describe_status(plan, status(plan, read_audit(args.audit))):
                print(line)
        print("nothing was changed: this was a dry run (--apply runs it)")
        return 0
    return _run(plan, args, config, rules)


def _run(plan: Plan, args: argparse.Namespace, config: Config, rules: Rules) -> int:
    if args.audit is None:
        print("--apply records every step so a stopped run can resume: name --audit. "
              "Nothing was changed.")
        return 2
    client = client_from(args, config)
    report = apply(
        plan,
        actions(client, keeper_check=default_checker(rules.dedupe.apply_keeper_check,
                                                      config=config)),
        audit=args.audit,
    )
    print(report)
    return 0 if report.ok else 1


def _dedupe(args: argparse.Namespace, config: Config) -> int:
    rules = Rules.from_config(config)
    if args.keeper_check:
        rules = dataclasses.replace(
            rules, dedupe=dataclasses.replace(rules.dedupe, keeper_check=args.keeper_check)
        )
    if args.plan_in is not None:
        return _apply_saved(args, config, rules)
    if args.apply and args.audit is None:
        print("--apply records every step so a stopped run can resume: name --audit. "
              "Nothing was changed.")
        return 2
    if args.apply and args.parked is None and config.paths.parked is None:
        print("--apply moves copies somewhere: name --parked, or set [paths] parked. "
              "Nothing was changed.")
        return 2

    reader = Client(config, dry_run=True)
    types = tuple(TYPES[t] for t in args.types) or ("Movie", "Episode")
    items: list[dict[str, Any]] = []
    for parent in args.parent or [None]:
        extra = {"parentId": parent} if parent else {}
        items += fetch_items(reader, types=types, fields=FIELDS, **extra)
    scan = find_groups(items)
    log.info("%d group(s) from %d row(s)", len(scan.groups), scan.items_seen)

    verdicts = resolve(scan.groups, rules, before_each=_gate(args, config),
                       config=config)
    parked = args.parked or config.paths.parked or DEFAULT_PARKED
    planned = build_plan(reader, verdicts, parked=parked, workers=args.jobs)
    if planned.unplanned:
        verdicts = [
            dataclasses.replace(v, verdict=BLOCKED,
                                reasons=(*v.reasons, planned.unplanned[v.group.key]))
            if v.group.key in planned.unplanned else v
            for v in verdicts
        ]
    plan = planned.plan

    print(render_table(verdicts, scan))
    print()
    print(plan.render())
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(_document(verdicts, rules, plan, scan), indent=1,
                       ensure_ascii=False, default=str) + "\n",
            encoding="utf-8", newline="\n")
    if args.tsv is not None:
        args.tsv.parent.mkdir(parents=True, exist_ok=True)
        args.tsv.write_text(render_tsv(verdicts), encoding="utf-8", newline="\n")
    if args.plan_out is not None:
        plan.save(args.plan_out)
        print(f"plan saved to {args.plan_out}")
    blocked = any(v.verdict == BLOCKED for v in verdicts)
    if not args.apply:
        print("nothing was changed: this was a dry run (--apply runs it)")
        return 1 if blocked else 0
    code = _run(plan, args, config, rules)
    return 1 if blocked else code
