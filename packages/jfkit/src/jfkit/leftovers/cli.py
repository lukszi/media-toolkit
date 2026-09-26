"""jfkit.leftovers.cli -- ``jfkit leftovers sweep`` and ``jfkit leftovers missing``.

Both walk the library folders the server lists (or the ones named, or the
ones the configuration names), one device after the other, asking the
device gate (:func:`jfkit.jobs.gate`) before each device; a device the gate
holds is not walked, and the run says so and exits 3.

``sweep`` is a dry run unless ``--apply`` is given, and ``--apply`` needs
``--audit`` and a parking directory. ``--plan-out`` saves the plan the dry
run printed; ``--plan`` applies a saved one, every step checked again as
it runs; the same ``--plan`` and ``--audit`` resume a run that stopped.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from mkvkit import integrity
from mkvkit.devices import Device
from mkvkit.lanes import map_by_device
from mkvkit.steps import add_plan_arguments, apply, load_plan
from mkvkit.walk import SkipReason

from .. import segments as segments_module
from ..client import Client
from ..commands import LIST_HELP, add_write_arguments, client_from, expand_lists
from ..config import Config
from ..jobs import gate as device_gate
from ..safedelete.catalogue import Catalogue, path_key
from ..safedelete.junk import load_rules
from ..safedelete.leftovers import CORRUPT, DEAD_FOLDER, RELEASE_JUNK, SAMPLE
from . import missing as missing_module
from .plan import Assessed, actions, assess, build_plan
from .scan import Finding, Scan, scan

__all__ = ["register"]

log = logging.getLogger(__name__)

CATEGORY_CHOICES = (RELEASE_JUNK, DEAD_FOLDER, SAMPLE, CORRUPT)

#: Exit status when a device gate held a device and part of the library was
#: not walked.
HELD = 3


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "leftovers", help="sweep release junk, dead release folders, samples and "
                          "unplayable files; report missing content",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    sweep = verbs.add_parser(
        "sweep", help="find leftovers and park the released categories",
        description="Walk the library folders and propose leftovers by category. "
                    "A dry run unless --apply is given.",
        epilog="exit status: 0 when everything planned was parked (or, in a dry "
               "run, when the walk finished); 1 when a step failed; 2 for a usage "
               "error; 3 when a device gate held part of the library.",
    )
    _add_walk_arguments(sweep)
    sweep.add_argument(
        "--release", action="append", default=[], choices=CATEGORY_CHOICES,
        metavar="CATEGORY",
        help="a category somebody decided may be parked: " + ", ".join(CATEGORY_CHOICES)
             + ". Nothing moves without one; the dry run shows what each would move",
    )
    sweep.add_argument(
        "--corrupt", action="append", default=[], metavar="PATH",
        help="a video to measure as a corrupt-unplayable candidate" + LIST_HELP,
    )
    sweep.add_argument(
        "--integrity", choices=["full", "quick"], default="full",
        help="how --corrupt files are measured: full (sampled, listed and decoded; "
             "the default) or quick (not decoded)",
    )
    sweep.add_argument("--parked", type=Path, metavar="DIR",
                       help="where things are parked (default: paths.parked)")
    sweep.add_argument("--format", choices=["text", "json", "tsv"], default="text")
    sweep.add_argument("--out", type=Path, metavar="DIR",
                       help="also write leftovers.json, leftovers.tsv and the plan here")
    sweep.add_argument("--limit", type=int, default=40, metavar="N",
                       help="lines per section in the text report (0: all)")
    add_plan_arguments(sweep)
    add_write_arguments(sweep)
    sweep.set_defaults(handler=_sweep)

    report = verbs.add_parser(
        "missing", help="rows with no file, gaps in seasons, releases with no item",
        description="Report what the library lacks. Read-only.",
        epilog="exit status: 0 when the report was made; 3 when a device gate "
               "held part of the library.",
    )
    _add_walk_arguments(report)
    report.add_argument("--format", choices=["table", "json", "tsv"], default="table")
    report.add_argument("--out", type=Path, metavar="DIR",
                        help="also write missing.txt, missing.tsv and missing.json here")
    report.set_defaults(handler=_missing)


def _add_walk_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "roots", nargs="*", metavar="ROOT",
        help="library folders to walk (default: every folder of every library the "
             "server lists, else paths.movies and paths.series)",
    )
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="leave out entries whose name or relative path matches")
    parser.add_argument("--exclude-path", action="append", default=[], metavar="PATH",
                        help="leave out this folder; links and junctions are never "
                             "entered anyway")
    parser.add_argument("--rules", type=Path, metavar="FILE",
                        help="a TOML file whose [leftovers] table widens or replaces "
                             "the junk rules")
    parser.add_argument("--no-gate", action="store_true",
                        help="walk without asking the device gate first")


# ------------------------------------------------------------------ shared
#: Libraries whose folders the server writes itself; never swept.
SERVER_OWNED = frozenset({"boxsets", "playlists"})


def _roots(args: argparse.Namespace, client: Client, config: Config) -> list[str]:
    """The folders named, else the server's media libraries, else the configured two.

    A collections or playlists library lives in the server's own data
    folder and holds what the server wrote there; it is left out, and said.
    """
    if args.roots:
        return [str(Path(r)) for r in expand_lists(args.roots)]
    try:
        libraries = client.get("/Library/VirtualFolders")
    except Exception:  # unreadable: fall back to the configured folders
        libraries = None
    served: list[str] = []
    for library in libraries if isinstance(libraries, list) else []:
        if not isinstance(library, dict):
            continue
        locations = [str(p) for p in library.get("Locations") or []]
        if str(library.get("CollectionType") or "").casefold() in SERVER_OWNED:
            for location in locations:
                print(f"left out: {location} ({library.get('CollectionType')}, the "
                      "server's own folder)", file=sys.stderr)
            continue
        served += locations
    if served:
        return served
    return [str(p) for p in (config.paths.movies, config.paths.series) if p]


def _gate(args: argparse.Namespace, client: Client) -> Callable[[Device], str | None]:
    if args.no_gate:
        return lambda _device: None
    try:
        running = [task.name for task in segments_module.running(client)]
    except Exception as exc:  # a server that cannot say is not a busy one
        log.warning("the server's running tasks could not be read: %s", exc)
        running = []

    def ask(device: Device) -> str | None:
        found = device_gate(device, running_tasks=running)
        print(found, file=sys.stderr)
        return None if found.open else "; ".join(found.reasons)
    return ask


def _walk(
    args: argparse.Namespace, client: Client, roots: Sequence[str], catalogue: Catalogue,
) -> Scan:
    rules = load_rules(args.rules)
    return scan(
        roots, catalogue, rules=rules, exclude=args.exclude,
        exclude_paths=args.exclude_path, gate=_gate(args, client),
    )


def _size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)  # pragma: no cover


def _walk_lines(found: Scan) -> list[str]:
    counts = ", ".join(f"{v} {k}" for k, v in sorted(found.counts.items()))
    lines = [f"walked {len(found.roots)} folder(s): {sum(found.counts.values())} "
             f"file(s) ({counts or 'none'})"]
    links = [s for s in found.skipped if s.reason is not SkipReason.EXCLUDED]
    excluded = [s for s in found.skipped if s.reason is SkipReason.EXCLUDED]
    lines += [f"  not entered: {s}" for s in links]
    lines += [f"  excluded: {s.path}" for s in excluded]
    lines += [f"  HELD {where}: {why}" for where, why in found.held.items()]
    return lines


# ------------------------------------------------------------------- sweep
def _measure(
    paths: Sequence[str], *, decode: bool, gate: Callable[[Device], str | None],
) -> tuple[list[Finding], dict[str, integrity.IntegrityReport]]:
    """Read each --corrupt file, one reader per device, gate asked before each."""
    def before(device: Device, _item: Path) -> None:
        held = gate(device)
        if held:
            raise RuntimeError(f"{device} is held: {held}")

    items = [Path(p) for p in paths]
    outcomes = map_by_device(
        lambda p: integrity.check(p, decode=decode), items,
        path_of=lambda p: p, before_each=before,
    )
    findings: list[Finding] = []
    reports: dict[str, integrity.IntegrityReport] = {}
    for outcome in outcomes:
        path = outcome.item
        if outcome.ok and outcome.result is not None:
            report = outcome.result
        else:
            report = integrity.IntegrityReport(
                path=path, evidence=False,
                problems=(f"not measured: {outcome.error}",),
            )
        reports[path_key(path)] = report
        size = report.size or 0
        findings.append(Finding(
            CORRUPT, path, False, f"integrity {report.verdict()}: "
            + ("; ".join(report.problems) or "no problem found"), size,
        ))
    return findings, reports


def _sweep(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    parked = args.parked or config.paths.parked
    if args.apply and (args.audit is None or parked is None):
        print("--apply needs --audit FILE and a parking directory (--parked or "
              "paths.parked). Nothing was moved.")
        return 2
    if args.plan_in is not None:
        if not args.apply:
            print(load_plan(args.plan_in).render())
            print("dry run: nothing was moved")
            return 0
        report = apply(load_plan(args.plan_in), actions(client, rules=load_rules(args.rules)),
                       audit=args.audit)
        print(report)
        return 0 if report.ok else 1

    catalogue = Catalogue.fetch(client)
    roots = _roots(args, client, config)
    if parked is not None and any(
        path_key(parked) == path_key(r) or path_key(parked).startswith(path_key(r) + "/")
        for r in roots
    ):
        print(f"the parking directory {parked} is inside a library folder; choose one "
              "outside every library. Nothing was moved.")
        return 2
    found = _walk(args, client, roots, catalogue)
    findings = list(found.findings)
    reports: dict[str, integrity.IntegrityReport] = {}
    if args.corrupt:
        measured, reports = _measure(
            expand_lists(args.corrupt), decode=args.integrity == "full",
            gate=_gate(args, client),
        )
        findings += measured
    assessed = assess(
        client, findings, released=args.release, catalogue=catalogue,
        rules=load_rules(args.rules), roots=roots, integrity=reports,
    )
    plan = build_plan(assessed, parked=parked or Path("parked"))
    if args.plan_out:
        plan.save(args.plan_out)
    if args.out:
        _write(args.out, found, assessed, plan.to_json())
    if args.format == "json":
        print(json.dumps(_document(found, assessed, plan.to_json()), ensure_ascii=False,
                         indent=1))
    elif args.format == "tsv":
        print(_tsv(found, assessed))
    else:
        print("\n".join(_text(found, assessed, limit=args.limit)))
        print(plan.render() if args.limit == 0 or len(plan.steps) <= args.limit
              else f"{plan.verb}: {len(plan.steps)} step(s), plan {plan.fingerprint} "
                   "(the steps are in --plan-out or --out)")
    if not args.apply:
        print("dry run: nothing was moved")
        return HELD if found.held else 0
    report = apply(plan, actions(client, rules=load_rules(args.rules)), audit=args.audit)
    print(report)
    if not report.ok:
        return 1
    return HELD if found.held else 0


def _text(found: Scan, assessed: Sequence[Assessed], *, limit: int) -> list[str]:
    lines = _walk_lines(found)

    def clip(rows: list[str], heading: str) -> None:
        lines.append("")
        lines.append(heading)
        shown = rows if not limit or len(rows) <= limit else rows[:limit]
        lines.extend(shown)
        if len(shown) < len(rows):
            lines.append(f"  ... {len(rows) - len(shown)} more line(s) in --out or --format")

    for category in CATEGORY_CHOICES:
        these = [a for a in assessed if a.finding.category == category]
        if not these:
            continue
        states: dict[str, int] = {}
        for one in these:
            states[one.state] = states.get(one.state, 0) + 1
        size = sum(a.finding.size for a in these)
        rows: list[str] = []
        for one in these:
            f = one.finding
            mark = {"park": "PARK", "not released": "listed", "refused": "REFUSED"}[one.state]
            rows.append(f"  {mark:8} {f.path}{os.sep if f.is_dir else ''}  "
                        f"({_size(f.size)}) -- {f.reason}")
            if f.evidence is not None:
                rows += [f"           evidence: {line}" for line in f.evidence.lines()]
            rows += [f"           {c}" for c in one.refusals if c.name != "the category "
                     "is one somebody released"]
        clip(rows, f"{category}: {len(these)} ({_size(size)}): "
                   + ", ".join(f"{v} {k}" for k, v in sorted(states.items())))
    folders = found.kept_folders
    if folders:
        rows = []
        for kept in folders:
            rows.append(f"  {kept.path}{os.sep}  ({_size(kept.size)}) -- {kept.reason}")
            if kept.evidence is not None:
                rows += [f"           evidence: {line}" for line in kept.evidence.lines()]
        clip(rows, f"kept folders, no video in them (never moved): {len(folders)}")
    unsure = found.unsure
    if unsure:
        clip([f"  {k.path}  ({_size(k.size)}) -- {k.reason}" for k in unsure],
             f"UNSURE, never moved: {len(unsure)}")
    lines.append("")
    return lines


def _document(found: Scan, assessed: Sequence[Assessed], plan: Any) -> dict[str, Any]:
    return {
        "roots": [str(r) for r in found.roots],
        "held": found.held,
        "counts": found.counts,
        "skipped": [{"path": str(s.path), "reason": s.reason.value, "detail": s.detail}
                    for s in found.skipped],
        "candidates": [a.as_dict() for a in assessed],
        "kept": [k.as_dict() for k in found.kept],
        "plan": plan,
    }


TSV_COLUMNS = ("section", "state", "category", "path", "is_dir", "size", "reason", "detail")


def _tsv(found: Scan, assessed: Sequence[Assessed]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(TSV_COLUMNS)
    for one in assessed:
        f = one.finding
        detail = "; ".join(
            [*(f.evidence.lines() if f.evidence else []),
             *(str(c) for c in one.refusals)]
        )
        writer.writerow(["candidate", one.state, f.category, str(f.path), f.is_dir,
                         f.size, f.reason, detail])
    for kept in found.kept:
        detail = "; ".join(kept.evidence.lines()) if kept.evidence else ""
        writer.writerow([kept.kind, "kept", "", str(kept.path), kept.kind != "unsure",
                         kept.size, kept.reason, detail])
    for skipped in found.skipped:
        writer.writerow(["skipped", skipped.reason.value, "", str(skipped.path), True, 0,
                         "not entered", skipped.detail])
    return buffer.getvalue().rstrip("\n")


def _write(folder: Path, found: Scan, assessed: Sequence[Assessed], plan: Any) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "leftovers.json").write_text(
        json.dumps(_document(found, assessed, plan), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n")
    (folder / "leftovers.tsv").write_text(_tsv(found, assessed) + "\n", encoding="utf-8",
                                          newline="\n")
    (folder / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n",
                                      encoding="utf-8", newline="\n")
    print(f"written to {folder}", file=sys.stderr)


# ----------------------------------------------------------------- missing
def _missing(args: argparse.Namespace, config: Config) -> int:
    client = client_from(args, config)
    catalogue = Catalogue.fetch(client)
    found = _walk(args, client, _roots(args, client, config), catalogue)
    virtual = missing_module.fetch_virtual(client)
    rows = missing_module.find_missing(catalogue, found, virtual=virtual)
    for line in _walk_lines(found):
        print(line)
    if not virtual:
        print("  the server listed no missing (virtual) episodes; season gaps are "
              "counted from the rows")
    if args.out:
        for path in missing_module.write(rows, args.out):
            print(f"  {path}")
    print(missing_module.render(rows, args.format))
    return HELD if found.held else 0
