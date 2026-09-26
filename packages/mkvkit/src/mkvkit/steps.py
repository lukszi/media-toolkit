"""mkvkit.steps -- plan, dry run, apply, audit, resume: one shape for every change.

Every verb in this repository that changes files or a server record goes
through the same four stages, and this module is the shared form of them for
verbs that change *many* things -- a rename that carries a dozen sidecars, a
replay of play state onto a hundred new items.

**A plan is data.** A :class:`Plan` is an ordered list of :class:`Step`
objects, each with an action name and JSON-serialisable parameters. It is
built by whatever decided the change, and nothing has happened yet. It can be
printed (:meth:`Plan.render`), saved (:meth:`Plan.save`), read back
(:func:`load_plan`) and handed to somebody else to read before anything runs.

**The dry run is the plan.** There is no separate code path that "simulates":
the dry run prints and saves exactly the steps :func:`apply` would execute, so
what was reviewed is what runs.

**Applying writes an audit as it goes.** One JSON object per line, appended
and flushed before and after every step (:class:`Audit`): the plan's
fingerprint, the step, the event (``plan``, ``start``, ``done``, ``failed``,
``skipped``) and whatever the action reported. It is the answer to "what
happened to X" months later, and it is what makes a resume possible.

**A partial failure resumes instead of being repaired by hand.** A pass over a
few hundred files that stops half way -- a file held open by another program,
an "access denied" on one folder -- is the normal case, not the exceptional
one. Run :func:`apply` again with the same plan and the same audit file: steps
the audit records as done are skipped, and a step whose action can tell that
its effect is already in place (:meth:`Action.done`) is recorded as done
without being run again. The plan's fingerprint ties an audit to one plan, so
a different plan never inherits another one's progress.

Actions are looked up by name in a mapping the caller provides.
:data:`FILE_ACTIONS` holds the file-system ones: ``mkdir``, ``rename`` (same
volume, never replaces), and ``copy`` and ``move`` (verified, through
:func:`mkvkit.transfer.verified_copy`). A verb adds its own for anything else.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FILE_ACTIONS",
    "SCHEMA",
    "Action",
    "ApplyReport",
    "Audit",
    "FunctionAction",
    "Plan",
    "Step",
    "StepResult",
    "add_plan_arguments",
    "apply",
    "describe_status",
    "load_plan",
    "read_audit",
    "rename_steps",
    "status",
]

log = logging.getLogger(__name__)

#: The version of the plan and audit documents. Bumped on any change a reader
#: of an old document would misread.
SCHEMA = 1


# ------------------------------------------------------------------ the plan
@dataclass(frozen=True)
class Step:
    """One change: an action name, its parameters, and a line for a person.

    ``id`` is unique within its plan and stable across runs of the same plan;
    it is what the audit refers to. ``params`` must be JSON-serialisable.
    """

    id: str
    action: str
    params: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "action": self.action,
                "params": dict(self.params), "summary": self.summary}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Step:
        return cls(id=str(raw["id"]), action=str(raw["action"]),
                   params=dict(raw.get("params") or {}), summary=str(raw.get("summary", "")))

    def __str__(self) -> str:
        return f"[{self.id}] {self.summary or self.action}"


@dataclass(frozen=True)
class Plan:
    """Everything a verb is about to do, in order, before any of it happens."""

    verb: str
    steps: tuple[Step, ...]
    notes: tuple[str, ...] = ()
    #: when the plan was made; not part of the fingerprint
    created: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"step id {step.id!r} appears twice in one plan")
            seen.add(step.id)

    @property
    def fingerprint(self) -> str:
        """A short digest of the verb and the steps: what ties an audit to a plan."""
        canonical = json.dumps(
            {"verb": self.verb, "steps": [s.to_json() for s in self.steps]},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "verb": self.verb, "created": self.created,
            "fingerprint": self.fingerprint, "notes": list(self.notes),
            "steps": [step.to_json() for step in self.steps],
        }

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8", newline="\n")
        return out

    def render(self) -> str:
        """The dry run: every step, in order, and the notes."""
        lines = [f"{self.verb}: {len(self.steps)} step(s), plan {self.fingerprint}"]
        lines += [f"  {step}" for step in self.steps]
        lines += [f"  note: {note}" for note in self.notes]
        return "\n".join(lines)


def load_plan(path: Path | str) -> Plan:
    """Read a saved plan, refusing one whose steps were edited after saving."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != SCHEMA:
        raise ValueError(f"{path}: plan schema {raw.get('schema')!r}, expected {SCHEMA}")
    plan = Plan(
        verb=str(raw["verb"]),
        steps=tuple(Step.from_json(step) for step in raw.get("steps") or []),
        notes=tuple(raw.get("notes") or ()),
        created=str(raw.get("created", "")),
    )
    stated = raw.get("fingerprint")
    if stated is not None and stated != plan.fingerprint:
        raise ValueError(
            f"{path}: the steps do not match the fingerprint the plan was saved with; "
            "a plan is not edited by hand after it was reviewed"
        )
    return plan


# ------------------------------------------------------------------- actions
@runtime_checkable
class Action(Protocol):
    """What executes one kind of step.

    ``run`` makes the change and returns what the audit should record about
    it. ``done`` answers whether the change is already in place, which is
    what lets a resumed run skip a step that finished before the audit could
    say so; an action that cannot tell returns False.
    """

    def run(self, step: Step) -> Mapping[str, Any] | None: ...

    def done(self, step: Step) -> bool: ...


@dataclass(frozen=True)
class FunctionAction:
    """An :class:`Action` made of two functions, the second optional."""

    runner: Callable[[Step], Mapping[str, Any] | None]
    checker: Callable[[Step], bool] | None = None

    def run(self, step: Step) -> Mapping[str, Any] | None:
        return self.runner(step)

    def done(self, step: Step) -> bool:
        return bool(self.checker and self.checker(step))


def _mkdir(step: Step) -> Mapping[str, Any]:
    Path(step.params["path"]).mkdir(parents=True, exist_ok=True)
    return {"path": step.params["path"]}


def _same_entry(src: Path, dst: Path) -> bool:
    """True when two spellings name one entry (a case-only rename)."""
    try:
        return src.exists() and dst.exists() and os.path.samefile(src, dst)
    except OSError:
        return False


def _rename(step: Step) -> Mapping[str, Any]:
    src, dst = Path(step.params["src"]), Path(step.params["dst"])
    if not src.exists() and not src.is_symlink():
        raise FileNotFoundError(f"nothing to rename at {src}")
    if (dst.exists() or dst.is_symlink()) and not _same_entry(src, dst):
        raise FileExistsError(f"{dst} exists and a rename never replaces anything")
    if step.params.get("parents"):
        dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file() and not _same_entry(src, dst):
        # a hard link refuses an existing target on every platform, which a
        # plain rename does not on all of them; fall back where links are not
        # available (a file system without them, a cross-volume "rename")
        try:
            os.link(src, dst)
        except OSError:
            if dst.exists():
                raise FileExistsError(f"{dst} appeared while renaming") from None
            os.rename(src, dst)
        else:
            try:
                os.unlink(src)
            except OSError:
                # the old name is held open: take the new one back, so the step
                # fails whole and a retry starts from where it started
                os.unlink(dst)
                raise
    else:
        os.rename(src, dst)
    return {"src": str(src), "dst": str(dst)}


def _renamed(step: Step) -> bool:
    src, dst = Path(step.params["src"]), Path(step.params["dst"])
    if _same_entry(src, dst):
        return any(p.name == dst.name for p in dst.parent.iterdir())
    return not src.exists() and dst.exists()


def _transfer(move: bool) -> Callable[[Step], Mapping[str, Any]]:
    def run(step: Step) -> Mapping[str, Any]:
        from .transfer import verified_copy

        report = verified_copy(
            step.params["src"], step.params["dst"], move=move,
            stage=step.params.get("stage"), dry_run=False,
        )
        if not report.ok:
            raise OSError("; ".join(report.problems))
        return {"src": str(report.source), "dst": str(report.destination),
                "digest": report.destination_digest, "size": report.size,
                "source_removed": report.source_removed}
    return run


def _moved(step: Step) -> bool:
    return not Path(step.params["src"]).exists() and Path(step.params["dst"]).is_file()


#: The file-system actions. ``rename`` is for one volume and never replaces an
#: existing entry; ``copy`` and ``move`` hash both sides and suit a move
#: between volumes. Every one knows whether its effect is already in place.
FILE_ACTIONS: Mapping[str, Action] = {
    "mkdir": FunctionAction(_mkdir, lambda s: Path(s.params["path"]).is_dir()),
    "rename": FunctionAction(_rename, _renamed),
    "copy": FunctionAction(_transfer(False), lambda s: Path(s.params["dst"]).is_file()),
    "move": FunctionAction(_transfer(True), _moved),
}


def rename_steps(
    pairs: Iterable[tuple[Path | str, Path | str]], *, prefix: str = "rename",
) -> list[Step]:
    """One ``rename`` step per (source, destination) pair, numbered in order."""
    return [
        Step(id=f"{prefix}:{index:04d}", action="rename",
             params={"src": str(src), "dst": str(dst)},
             summary=f"rename {src} -> {dst}")
        for index, (src, dst) in enumerate(pairs, start=1)
    ]


# --------------------------------------------------------------------- audit
@dataclass
class Audit:
    """An append-only JSON-lines log, flushed after every line.

    With no path it only keeps the entries in memory, which is what a dry run
    and a test want.
    """

    path: Path | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {"ts": self.clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": event}
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                handle.flush()
        return entry


def read_audit(path: Path | str) -> list[dict[str, Any]]:
    """Every entry of an audit file; a torn last line (a crash mid-write) is dropped."""
    target = Path(path)
    if not target.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("%s: an unreadable audit line was ignored", target)
    return out


def status(plan: Plan, entries: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Each step's last recorded state for this plan: done, failed, started or pending."""
    out = {step.id: "pending" for step in plan.steps}
    for entry in entries:
        if entry.get("plan") != plan.fingerprint or entry.get("step") not in out:
            continue
        event = entry.get("event")
        if event in ("done", "failed"):
            out[str(entry["step"])] = str(event)
        elif event == "start":
            out[str(entry["step"])] = "started"
    return out


# --------------------------------------------------------------------- apply
@dataclass(frozen=True)
class StepResult:
    step: Step
    #: done, skipped (done in an earlier run), failed, or pending (never reached)
    state: str
    detail: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ApplyReport:
    plan: Plan
    results: tuple[StepResult, ...]
    audit: Path | None = None

    def _count(self, state: str) -> int:
        return sum(1 for r in self.results if r.state == state)

    @property
    def ok(self) -> bool:
        return all(r.state in ("done", "skipped") for r in self.results)

    @property
    def failed(self) -> list[StepResult]:
        return [r for r in self.results if r.state == "failed"]

    def __str__(self) -> str:
        lines = [
            f"{self.plan.verb}: plan {self.plan.fingerprint}: {self._count('done')} done, "
            f"{self._count('skipped')} already done earlier, {self._count('failed')} "
            f"failed, {self._count('pending')} not reached"
        ]
        lines += [f"  FAILED {r.step}: {r.error}" for r in self.failed]
        if not self.ok and self.audit is not None:
            lines.append(
                f"  run the same plan again with --audit {self.audit} to resume; "
                "finished steps are skipped"
            )
        return "\n".join(lines)


def apply(
    plan: Plan,
    actions: Mapping[str, Action],
    *,
    audit: Audit | Path | str | None = None,
    resume: bool = True,
    stop_on_error: bool = True,
    attempts: int = 1,
    retry_on: tuple[type[BaseException], ...] = (PermissionError,),
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ApplyReport:
    """Execute a plan step by step, recording each one, resuming where it stopped.

    Every action a step names must be in ``actions``; that is checked before
    the first step runs. With ``resume`` (the default) the audit is read
    first and steps it records as done for this plan are skipped. A step whose
    action reports its effect already in place is recorded as done without
    running. An error in ``retry_on`` is retried ``attempts - 1`` times with a
    doubling back-off -- a file another program briefly holds open is the case
    it exists for. After a failure the remaining steps are left pending unless
    ``stop_on_error`` is false.
    """
    missing = sorted({s.action for s in plan.steps} - set(actions))
    if missing:
        raise KeyError(f"no action for: {', '.join(missing)}")
    writer = audit if isinstance(audit, Audit) else Audit(
        Path(audit) if audit is not None else None
    )
    earlier = status(plan, read_audit(writer.path)) if (resume and writer.path) else {}
    writer.record("plan", plan=plan.fingerprint, verb=plan.verb, steps=len(plan.steps),
                  resumed=any(v == "done" for v in earlier.values()) or None,
                  document=plan.to_json())

    results: list[StepResult] = []
    stopped = False
    for step in plan.steps:
        if stopped:
            results.append(StepResult(step, "pending"))
            continue
        if earlier.get(step.id) == "done":
            writer.record("skipped", plan=plan.fingerprint, step=step.id,
                          action=step.action, reason="done in an earlier run")
            results.append(StepResult(step, "skipped"))
            continue
        action = actions[step.action]
        writer.record("start", plan=plan.fingerprint, step=step.id, action=step.action,
                      summary=step.summary, params=dict(step.params))
        try:
            if resume and action.done(step):
                detail: Mapping[str, Any] | None = {"already": "the change was in place"}
            else:
                detail = _run_with_retry(action, step, attempts, retry_on, backoff_s, sleep)
        except Exception as exc:  # recorded, then reported; never lost
            message = f"{type(exc).__name__}: {exc}"
            writer.record("failed", plan=plan.fingerprint, step=step.id,
                          action=step.action, error=message)
            log.error("%s failed: %s", step, message)
            results.append(StepResult(step, "failed", error=message))
            stopped = stop_on_error
            continue
        writer.record("done", plan=plan.fingerprint, step=step.id, action=step.action,
                      detail=dict(detail) if detail else None)
        results.append(StepResult(step, "done", detail=detail))
    return ApplyReport(plan=plan, results=tuple(results), audit=writer.path)


def _run_with_retry(
    action: Action, step: Step, attempts: int,
    retry_on: tuple[type[BaseException], ...], backoff_s: float,
    sleep: Callable[[float], None],
) -> Mapping[str, Any] | None:
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return action.run(step)
        except retry_on as exc:
            if attempt >= attempts:
                raise
            delay = backoff_s * (2.0 ** (attempt - 1))
            log.warning("%s: %s, retrying in %.1fs", step, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


# ------------------------------------------------------------------ the CLI
def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """The switches a verb built on plans shares: where the plan and audit go.

    ``--plan-out`` saves the dry run's plan for review; ``--plan`` applies a
    saved one instead of planning again; ``--audit`` is the JSON-lines log an
    applied run appends to and a resumed run reads.
    """
    parser.add_argument("--plan-out", type=Path, metavar="PATH",
                        help="save the plan as JSON (the dry run's whole output)")
    parser.add_argument("--plan", dest="plan_in", type=Path, metavar="PATH",
                        help="apply this saved plan rather than planning again")
    parser.add_argument("--audit", type=Path, metavar="PATH",
                        help="append every step to this JSON-lines file; run the same "
                             "plan with the same file to resume after a failure")


def describe_status(plan: Plan, states: Mapping[str, str]) -> Sequence[str]:
    """One line per step with its recorded state, for ``steps status``."""
    return [f"  {states.get(step.id, 'pending'):8} {step}" for step in plan.steps]


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """``mkvkit steps show|status|apply``: read a saved plan, and resume a file plan."""
    from .commands import add_write_arguments

    parser = subparsers.add_parser(
        "steps", help="show a saved plan, its progress, or apply a file plan",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")

    show = verbs.add_parser("show", help="print a saved plan, as its dry run did")
    show.add_argument("plan", type=Path)
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=_cli_show)

    state = verbs.add_parser("status", help="each step's state according to an audit")
    state.add_argument("plan", type=Path)
    state.add_argument("--audit", type=Path, required=True, metavar="PATH")
    state.set_defaults(handler=_cli_status)

    run = verbs.add_parser(
        "apply",
        help="run a saved plan of file steps (mkdir, rename, copy, move), resuming "
             "from its audit",
        description="Run a saved plan whose steps are all file-system steps. "
                    "Finished steps recorded in the audit are skipped.",
        epilog="exit status: 0 when every step is done, 1 when a step failed or "
               "the dry run was printed with steps still to do, 2 for a usage error "
               "or a plan with a step this verb cannot run.",
    )
    run.add_argument("plan", type=Path)
    run.add_argument("--audit", type=Path, required=True, metavar="PATH")
    run.add_argument("--attempts", type=int, default=1, metavar="N",
                     help="tries per step when access is refused (default 1)")
    add_write_arguments(run)
    run.set_defaults(handler=_cli_apply)


def _cli_show(args: argparse.Namespace, _config: object) -> int:
    plan = load_plan(args.plan)
    print(json.dumps(plan.to_json(), indent=1, ensure_ascii=False) if args.json
          else plan.render())
    return 0


def _cli_status(args: argparse.Namespace, _config: object) -> int:
    plan = load_plan(args.plan)
    states = status(plan, read_audit(args.audit))
    print(f"{plan.verb}: plan {plan.fingerprint}")
    for line in describe_status(plan, states):
        print(line)
    return 0 if all(v == "done" for v in states.values()) else 1


def _cli_apply(args: argparse.Namespace, _config: object) -> int:
    plan = load_plan(args.plan)
    unknown = sorted({s.action for s in plan.steps} - set(FILE_ACTIONS))
    if unknown:
        print(f"this plan has steps only its own verb can run: {', '.join(unknown)}")
        return 2
    if not args.apply:
        states = status(plan, read_audit(args.audit))
        print(plan.render())
        for line in describe_status(plan, states):
            print(line)
        print("nothing was changed: this was a dry run (--apply runs it)")
        return 0 if all(v == "done" for v in states.values()) else 1
    report = apply(plan, FILE_ACTIONS, audit=args.audit, attempts=args.attempts)
    print(report)
    return 0 if report.ok else 1
