"""jfkit.rename.verb -- ``jfkit rename``: one audited plan from mapping to verified end state.

The dry run (the default) predicts every target, lists every file that moves
or is parked, finds the items on the server, counts the watched state that
would be carried and prints the plan; it writes nothing (``--plan-out`` saves
the plan when asked). ``--apply`` needs ``--work DIR``, which holds everything
a run leaves behind:

* ``rename.json`` -- the mapping, the options, the videos and their old ids;
* ``snapshot.json`` -- every user's watched state, taken before anything moved;
* ``plan.json`` -- the file and notification steps, fingerprinted;
* ``audit.jsonl`` -- every step, the wait, the replay and the checks;
* ``ids.json`` -- old identifier -> new identifier;
* ``report.txt`` -- the end-state report.

Running ``--apply`` again with the same ``--work`` resumes: the saved plan is
applied again (finished steps are skipped), the saved snapshot is used, and the
wait, the replay and the checks run again -- each of them is safe to repeat.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkvkit.lanes import DEFAULT_WORKERS
from mkvkit.steps import FILE_ACTIONS, Audit, apply, load_plan

from .. import userdata
from ..client import Client
from ..commands import add_write_arguments, client_from, server_configured
from ..config import Config
from ..naming import parse
from ..refresh import request_refresh
from . import server
from .plan import (
    NFO_POLICIES,
    Expect,
    FilePlan,
    Options,
    Pair,
    Video,
    mapping_digest,
    parse_expect,
    plan_files,
    read_mapping,
)

__all__ = ["REFRESH_MODES", "register"]

log = logging.getLogger(__name__)

#: What ``--refresh`` can ask for once the new items are there.
REFRESH_MODES = ("none", "items", "series")

#: Where a work folder keeps each document.
RENAME, SNAPSHOT, PLAN, AUDIT, IDS, REPORT = (
    "rename.json", "snapshot.json", "plan.json", "audit.jsonl", "ids.json", "report.txt",
)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "rename",
        help="rename videos or folders as one audited plan, sidecars and watched "
             "state included",
        description="Rename videos or folders in a library: predict how the server "
                    "reads every target, carry the sidecars, park stale .nfo files, "
                    "snapshot every user's watched state, rename, notify the deepest "
                    "changed folders, wait for the new items, replay the watched state "
                    "and verify the end state.",
        epilog="exit status: 0 when the dry run found nothing to refuse, or the "
               "rename was applied and verified; 1 when the plan is refused, a step "
               "failed, the server did not catch up in time or the end state differs "
               "(run again with the same --work to resume); 2 for a usage error.",
    )
    parser.add_argument("old", nargs="?", help="the video or folder to rename")
    parser.add_argument("new", nargs="?", help="its new path")
    parser.add_argument("--expect", metavar="SPEC",
                        help="what the new name should be read as: S01E03, E03, "
                             "S01E03-E04, extra, extra:TYPE, none or movie (default: "
                             "inferred from the new name)")
    parser.add_argument("--map", dest="mapping", metavar="FILE",
                        help="many renames: tab-separated old, new and an optional "
                             "intention per line; '-' reads standard input")
    parser.add_argument("--park", type=Path, metavar="DIR",
                        help="where stale .nfo files go, outside every library "
                             "(default: [paths] parked/rename)")
    parser.add_argument("--nfo", choices=NFO_POLICIES, default="stale",
                        help="park the .nfo when the reading changes (stale, the "
                             "default), always (park), or never (carry)")
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="a library root besides those the server and the "
                             "configuration name")
    parser.add_argument("--parent", action="append", default=[], metavar="ID",
                        help="the series (or container) the items are in, instead of "
                             "searching for it")
    parser.add_argument("--files-only", action="store_true",
                        help="no server: predict, carry, park and rename only")
    parser.add_argument("--allow-library-scan", action="store_true",
                        help="notify a library root, or a folder below one the server "
                             "does not know, although that validates the whole library")
    parser.add_argument("--refresh", choices=REFRESH_MODES, default="none",
                        help="once the new items are there, queue a non-replacing "
                             "refresh of each (items) or of their series (series)")
    parser.add_argument("--wait-timeout", type=float, default=600.0, metavar="S",
                        help="how long to wait for the new items (default 600)")
    parser.add_argument("--poll", type=float, default=10.0, metavar="S",
                        help="seconds between reads while waiting (default 10)")
    parser.add_argument("--idle-timeout", type=float, default=0.0, metavar="S",
                        help="wait this long for playback to stop before starting "
                             "(default 0: refuse at once while somebody is watching)")
    parser.add_argument("--attempts", type=int, default=3, metavar="N",
                        help="tries per file step when access is refused, as by a file "
                             "another program holds open (default 3)")
    parser.add_argument("--work", type=Path, metavar="DIR",
                        help="where an applied run keeps its plan, snapshot, audit and "
                             "report; the same folder again resumes")
    parser.add_argument("--plan-out", type=Path, metavar="PATH",
                        help="save the dry run's plan as JSON")
    parser.add_argument("--jobs", type=int, default=DEFAULT_WORKERS, metavar="N",
                        help=f"requests sent at once (default {DEFAULT_WORKERS})")
    add_write_arguments(parser)
    parser.set_defaults(handler=_handler)


# ------------------------------------------------------------------ input
def _pairs(args: argparse.Namespace) -> list[Pair]:
    if args.mapping and (args.old or args.new):
        raise ValueError("name one rename as OLD NEW or many with --map, not both")
    if args.mapping:
        if args.expect:
            raise ValueError("--expect is for one rename; a mapping names the intention "
                             "in its third column")
        text = (sys.stdin.read() if args.mapping == "-"
                else Path(args.mapping).read_text(encoding="utf-8-sig"))
        pairs = read_mapping(text, origin=args.mapping)
        if not pairs:
            raise ValueError(f"{args.mapping}: no renames in it")
        return pairs
    if args.old and args.new:
        return [Pair(Path(args.old), Path(args.new),
                     parse_expect(args.expect) if args.expect else None)]
    if args.old or args.new:
        raise ValueError("a rename needs both OLD and NEW")
    return []


def _roots(args: argparse.Namespace, config: Config,
           served: dict[str, str | None] | None) -> dict[str, str | None]:
    out: dict[str, str | None] = dict(served or {})
    if config.paths.movies:
        out.setdefault(str(config.paths.movies), "movies")
    if config.paths.series:
        out.setdefault(str(config.paths.series), "tvshows")
    for root in args.root:
        out.setdefault(str(root), None)
    return out


def _park(args: argparse.Namespace, config: Config) -> Path | None:
    if args.park is not None:
        return Path(args.park)
    return config.paths.parked / "rename" if config.paths.parked else None


# ------------------------------------------------------------------ output
def _table(files: FilePlan) -> list[str]:
    lines = [f"{len(files.pairs)} rename(s), {len(files.videos)} video(s), "
             f"{len(files.moves)} move(s), {len(files.parked)} .nfo parked"]
    refused = {p.path for p in files.problems}
    for video in files.videos:
        state = "REFUSED" if str(video.new) in refused or video.problems else "ok"
        lines.append(f"  {state:8} intended {video.expect!s:<14} read "
                     f"{video.after.describe():<34} {video.new}")
        lines.append(f"  {'':8} was      {video.before.describe():<49} {video.old}")
    for src, dst in files.parked:
        lines.append(f"  park     {src} -> {dst}")
    return lines


def _say(lines: Sequence[str] | str) -> None:
    print(lines if isinstance(lines, str) else "\n".join(lines))


# ------------------------------------------------------------------ the verb
@dataclass
class _Run:
    args: argparse.Namespace
    config: Config
    client: Client | None
    sleep: Callable[[float], None]

    @property
    def work(self) -> Path:
        assert self.args.work is not None
        return Path(self.args.work)


#: How a run waits: between reads, for playback and before a retry. A test
#: replaces it.
_sleep: Callable[[float], None] = time.sleep


def _handler(args: argparse.Namespace, config: Config) -> int:
    try:
        pairs = _pairs(args)
    except (ValueError, OSError) as exc:
        print(exc)
        return 2
    if args.apply and args.work is None:
        print("--apply keeps its plan, snapshot and audit in a folder: name --work DIR. "
              "Nothing was changed.")
        return 2
    if not args.files_only and not server_configured(config):
        print("no server is configured: pass --files-only to rename files without "
              "carrying watched state. Nothing was changed.")
        return 2
    client = None if args.files_only else client_from(args, config)
    run = _Run(args, config, client, _sleep)
    if args.apply and (run.work / PLAN).is_file():
        return _resume(run, pairs)
    if not pairs:
        print("nothing to rename: name OLD NEW, or --map FILE")
        return 2
    return _start(run, pairs)


def _plan(run: _Run, pairs: list[Pair]) -> tuple[FilePlan, server.Prepared | None, int]:
    """The file plan and the server's part of it; the exit code if it is refused."""
    served = None
    if run.client is not None:
        served = server.library_kinds(run.client)
        if served is None:
            print("the server's library folders could not be read, so no notification "
                  "can be shown to avoid a library root.")
            return _empty(pairs), None, 1
    options = Options(park=_park(run.args, run.config), nfo=run.args.nfo,
                      roots=_roots(run.args, run.config, served),
                      allow_library_scan=run.args.allow_library_scan)
    files = plan_files(pairs, options)
    _say(_table(files))
    prepared = None
    if run.client is not None and files.videos:
        prepared = server.prepare(run.client, files, roots=list(options.roots),
                                  parents=run.args.parent,
                                  allow_library_scan=run.args.allow_library_scan)
        print(f"server: {len(prepared.old_ids)} of {len(files.videos)} old video(s) are "
              f"items; {len(prepared.scope)} item(s) in scope for watched state")
    problems = [*files.problems, *(prepared.problems if prepared else ())]
    if problems:
        print(f"REFUSED: {len(problems)} problem(s); nothing was changed")
        _say([f"  {p}" for p in problems])
        return files, prepared, 1
    return files, prepared, 0


def _empty(pairs: list[Pair]) -> FilePlan:
    return FilePlan(pairs=tuple(pairs), videos=(), moves=(), parked=(), notify=(),
                    series=(), steps=())


def _start(run: _Run, pairs: list[Pair]) -> int:
    files, prepared, refused = _plan(run, pairs)
    if refused:
        return refused
    plan = files.plan(prepared.steps if prepared else (),
                      prepared.notes if prepared else ())
    if not run.args.apply:
        return _dry_run(run, files, prepared, plan)
    client = run.client
    snap = None
    if client is not None and prepared is not None:
        if not _idle(run, client):
            return 1
        snap = userdata.snapshot(client, prepared.scope, workers=run.args.jobs) \
            if prepared.scope else None
    run.work.mkdir(parents=True, exist_ok=True)
    if snap is not None:
        snap.save(run.work / SNAPSHOT)
        print(f"snapshot: {len(snap.items)} item(s), {len(snap.users)} user(s), written to "
              f"{run.work / SNAPSHOT}")
    document = {
        "digest": files.digest,
        "pairs": [p.to_json() for p in pairs],
        "videos": [v.to_json() for v in files.videos],
        "old_ids": dict(prepared.old_ids) if prepared else {},
        "parents": list(prepared.parents) if prepared else [],
        "parents_given": list(run.args.parent),
        "files": files.to_json(),
    }
    (run.work / RENAME).write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8", newline="\n")
    plan.save(run.work / PLAN)
    print(plan.render())
    return _execute(run, _videos(document), document)


def _dry_run(run: _Run, files: FilePlan, prepared: server.Prepared | None,
             plan: Any) -> int:
    if run.client is not None and prepared is not None and prepared.scope:
        snap = userdata.snapshot(run.client, prepared.scope, workers=run.args.jobs)
        rows = sum(1 for item in snap.items for s in item.states.values() if not s.blank)
        print(f"watched state: {rows} (item, user) row(s) with state across "
              f"{len(snap.users)} user(s) would be snapshot and replayed")
    print(plan.render())
    if run.args.plan_out is not None:
        print(f"plan written to {plan.save(run.args.plan_out)}")
    print("nothing was changed: this was a dry run (--apply --work DIR runs it)")
    return 0


def _idle(run: _Run, client: Client) -> bool:
    try:
        client.wait_idle(timeout_s=run.args.idle_timeout,
                         poll_s=max(1.0, min(15.0, run.args.idle_timeout / 4 or 1.0)),
                         sleep=run.sleep)
    except TimeoutError as exc:
        print(f"somebody is watching ({exc}); nothing was changed. Try again later, or "
              "give --idle-timeout.")
        return False
    return True


def _videos(document: dict[str, Any]) -> list[Video]:
    return [
        Video(Path(raw["old"]), Path(raw["new"]), Expect.from_json(raw["expect"]),
              parse(raw["old"]), parse(raw["new"]), int(raw["pair"]))
        for raw in document["videos"]
    ]


def _resume(run: _Run, pairs: list[Pair]) -> int:
    document = json.loads((run.work / RENAME).read_text(encoding="utf-8"))
    if pairs and mapping_digest(pairs) != document["digest"]:
        print(f"{run.work} holds the run of another mapping; name a new --work folder, "
              "or run it without a mapping to resume it. Nothing was changed.")
        return 2
    plan = load_plan(run.work / PLAN)
    print(f"resuming plan {plan.fingerprint} from {run.work}")
    return _execute(run, _videos(document), document)


def _execute(run: _Run, videos: list[Video], document: dict[str, Any]) -> int:
    plan = load_plan(run.work / PLAN)
    audit_path = run.work / AUDIT
    actions = dict(FILE_ACTIONS)
    if run.client is not None:
        actions.update(server.actions(run.client))
    report = apply(plan, actions, audit=audit_path, attempts=run.args.attempts,
                   sleep=run.sleep)
    print(report)
    if not report.ok:
        return 1
    client = run.client
    if client is None:
        _write_report(run, [str(report), "files only: no server was told, nothing was "
                                         "waited for, no watched state was carried"])
        return 0
    audit = Audit(audit_path)
    parents = [str(p) for p in document.get("parents_given") or []]
    arrival = server.wait_for(client, videos, parents=parents,
                              timeout_s=run.args.wait_timeout, poll_s=run.args.poll,
                              sleep=run.sleep)
    audit.record("rename.wait", plan=plan.fingerprint, ok=arrival.ok, polls=arrival.polls,
                 missing=[str(p) for p in arrival.missing],
                 lingering=[str(p) for p in arrival.lingering])
    print(arrival)
    if not arrival.ok:
        print(f"run the same command again with --work {run.work} to wait again; the "
              "renames are done and are not repeated")
        return 1
    mapping = server.id_map(videos, document.get("old_ids") or {}, arrival)
    (run.work / IDS).write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8",
                                newline="\n")
    _refresh(run, client, audit, plan.fingerprint, mapping, arrival)
    lines = [str(report), str(arrival)]
    replay_ok = True
    if (run.work / SNAPSHOT).is_file():
        snap = userdata.Snapshot.load(run.work / SNAPSHOT)
        scope = arrival.catalogue.ids
        replay = userdata.plan_replay(client, snap, mapping, scope=scope,
                                      workers=run.args.jobs)
        print(replay.render())
        applied = apply(replay, userdata.actions(client), audit=audit_path)
        print(applied)
        checked = userdata.verify(client, snap, mapping, scope=scope, workers=run.args.jobs)
        print(checked)
        audit.record("rename.replay", plan=replay.fingerprint, ok=applied.ok and checked.ok,
                     rows=checked.rows, differ=len(checked.mismatches),
                     dates_left=len(checked.dates_left))
        replay_ok = applied.ok and checked.ok
        lines += [replay.render(), str(applied), str(checked)]
    end = server.verify_end(videos, arrival)
    print(end)
    audit.record("rename.verify", plan=plan.fingerprint, ok=end.ok,
                 problems=list(end.problems))
    lines.append(str(end))
    _write_report(run, lines)
    return 0 if replay_ok and end.ok else 1


def _refresh(run: _Run, client: Client, audit: Audit, fingerprint: str,
             mapping: dict[str, str], arrival: server.Arrival) -> None:
    mode = run.args.refresh
    if mode == "none":
        return
    if mode == "items":
        targets = sorted(set(mapping.values()))
    else:
        targets = sorted({str(item.get("SeriesId")) for item in arrival.catalogue.items.values()
                          if str(item["Id"]) in set(mapping.values()) and item.get("SeriesId")})
    for item_id in targets:
        request_refresh(client, item_id)
    audit.record("rename.refresh", plan=fingerprint, mode=mode, items=targets)
    print(f"refresh queued (not replacing) for {len(targets)} item(s)")


def _write_report(run: _Run, lines: Sequence[str]) -> None:
    path = run.work / REPORT
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"report written to {path}")
