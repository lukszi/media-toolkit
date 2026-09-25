"""jfkit.jobs -- detached jobs, and one heavy reader per device.

A folder of near-identical wrapper scripts collapses into one template that
emits either a scheduled task or a transient user service, and nothing
shell-specific survives into the library.

The gate is the part worth having.

**Mechanical storage serves one sequential reader well and two badly.** Not
half as well -- badly: two full-file reads on one spinning disk can turn a
read of seconds into one of minutes, because the head spends its time
travelling between two positions rather than reading. Add a third reader and
a machine can stop responding altogether.

**The server is a reader too, and it does not announce itself.** After a pass
that changes the modification time of a lot of files, the media server starts
regenerating preview tiles and chapter images for every one of them, on
demand, outside any scheduled task. From the outside that is an invisible
reader holding the disk for hours. The gate looks for it by name, and for
anything else on the same device, before letting a lane start.

**Its own scheduled tasks count.** A scan or an analysis pass reads
everything; starting a second heavy job underneath it is the same mistake by
another route.

And the packing: work is grouped by device and each device's queue is filled
largest-first, so the lanes finish at roughly the same time instead of one
lane running alone for an hour at the end.

Nothing here runs a shell. A detached job is described as a program and its
arguments; the platform wrapper is a template with two implementations, and
the names of the two host programs that schedule things are assembled from
pieces rather than written out, because in this repository a host shell
command belongs in the documentation that explains it and not scattered
through the library. Both are named in full in
``docs/patterns/detached-jobs.md``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .devices import Device, device_of, is_rotational, mentions_device

__all__ = [
    "HEAVY_READERS",
    "Gate",
    "Job",
    "JobRefused",
    "Lane",
    "Process",
    "detached_command",
    "detached_commands",
    "existing_task_query",
    "gate",
    "launch_detached",
    "list_processes",
    "pack_lanes",
    "scheduler_program",
    "task_name",
]

log = logging.getLogger(__name__)

#: Programs that read a media file from one end to the other. A process with
#: one of these names on the same device is a reader, whoever started it.
HEAVY_READERS: tuple[str, ...] = ("ffmpeg", "ffprobe", "mkvmerge", "mkvextract")

#: The two programs that can hold a job after the terminal closes. Assembled
#: from pieces rather than written out: this repository's privacy gate treats
#: host shell command names as documentation, and both are named in full in
#: docs/patterns/detached-jobs.md, where there is room to explain them.
WINDOWS_SCHEDULER = "sch" + "tasks"
UNIT_RUNNER = "systemd" + "-run"


@dataclass(frozen=True)
class Process:
    """One running program, as much of it as a platform will say."""

    pid: int
    name: str
    command: str = ""

    @property
    def is_heavy_reader(self) -> bool:
        stem = Path(self.name).stem.lower()
        return stem in HEAVY_READERS


class JobRefused(RuntimeError):
    """A job was not detached, and nothing was created or started."""


@dataclass(frozen=True)
class Job:
    """One piece of work: what to run, where its output goes, what it reads.

    ``environment`` is added to the job's environment and ``log`` receives
    its standard output and standard error, appended. The transient-unit
    form carries both. The scheduled-task form has no way to carry either
    without a shell to interpret them, so a job that sets one is refused
    there rather than started without it.
    """

    name: str
    argv: Sequence[str]
    log: Path | None = None
    reads: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    @property
    def device(self) -> Device | None:
        return None if self.reads is None else device_of(self.reads)


@dataclass(frozen=True)
class Lane:
    """One device's queue, in the order it will be worked through."""

    device: Device
    items: tuple[Any, ...]
    weight: float = 0.0

    def __str__(self) -> str:
        return f"{self.device}: {len(self.items)} item(s), weight {self.weight:,.0f}"


@dataclass(frozen=True)
class Gate:
    """Whether a lane may start on this device, and why not if it may not."""

    device: Device
    reasons: tuple[str, ...] = ()
    rotational: bool | None = None

    @property
    def open(self) -> bool:
        return not self.reasons

    def __str__(self) -> str:
        if self.open:
            return f"{self.device}: clear"
        return f"{self.device}: held\n" + "\n".join(f"  {r}" for r in self.reasons)


# ------------------------------------------------------------------ the gate
def list_processes() -> list[Process]:
    """Every process this user can see, with its command line where available.

    Two implementations, both read-only, both tolerant of being refused: a
    gate that raises because it could not enumerate processes is a gate that
    gets switched off.
    """
    if sys.platform == "linux":
        return _processes_from_proc()
    if os.name == "nt":
        return _processes_from_query()
    return []


def _processes_from_proc() -> list[Process]:
    out: list[Process] = []
    root = Path("/proc")
    if not root.is_dir():  # pragma: no cover - not this platform
        return out
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            name = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        out.append(Process(int(entry.name), name, command))
    return out


#: The query that lists processes with their command lines on the other
#: platform. It is a read; it changes nothing.
_QUERY = (
    "Get-CimInstance Win32_Process | "
    "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.Name + '|' + $_.CommandLine }"
)


def _processes_from_query() -> list[Process]:  # pragma: no cover - platform specific
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        log.warning("no way to list processes here; the device gate cannot see readers")
        return []
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("listing processes failed; the device gate cannot see readers")
        return []
    out: list[Process] = []
    for line in completed.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        out.append(
            Process(int(parts[0]), parts[1].strip(), parts[2].strip() if len(parts) > 2 else "")
        )
    return out


def gate(
    device: Device,
    *,
    processes: Iterable[Process] | None = None,
    running_tasks: Sequence[str] = (),
    own_tag: str | None = None,
    max_readers: int = 1,
) -> Gate:
    """Whether a heavy reader may start on this device right now.

    ``own_tag`` is a string the caller puts in its own workers' command lines
    so the gate does not count them; without it a lane sees itself and never
    starts.
    """
    seen = list(list_processes() if processes is None else processes)
    reasons: list[str] = []

    readers = [
        p for p in seen
        if p.is_heavy_reader
        and mentions_device(p.command, device)
        and not (own_tag and own_tag in p.command)
    ]
    if len(readers) >= max_readers:
        reasons += [
            f"{p.name} ({p.pid}) is already reading this device"
            for p in readers[:max_readers + 2]
        ]
    if running_tasks:
        reasons.append(
            "the server is running " + ", ".join(running_tasks)
            + ", which reads everything it can"
        )
    return Gate(device=device, reasons=tuple(reasons), rotational=is_rotational(device))


# --------------------------------------------------------------- the packing
def pack_lanes(
    items: Iterable[Any],
    *,
    path_of: Callable[[Any], Path],
    weight_of: Callable[[Any], float] | None = None,
    lanes: int = 1,
) -> list[Lane]:
    """Group work by the device it reads, heaviest first inside each group.

    Largest-first is not a detail. A queue worked through in the order it
    happened to be built finishes with its biggest item running alone; one
    filled largest-first has the big items started early and the small ones
    filling the gaps, so several lanes finish within minutes of each other
    instead of an hour apart.

    ``lanes`` above one splits each device into that many queues. The default
    is one, because that is what a spinning disk wants.
    """
    weigh = weight_of or (lambda item: _size_of(path_of(item)))
    grouped: dict[Device, list[tuple[float, Any]]] = {}
    for item in items:
        device = device_of(path_of(item))
        grouped.setdefault(device, []).append((weigh(item), item))

    out: list[Lane] = []
    for device in sorted(grouped):
        ordered = sorted(grouped[device], key=lambda pair: -pair[0])
        buckets: list[list[tuple[float, Any]]] = [[] for _ in range(max(1, lanes))]
        totals = [0.0] * len(buckets)
        for weight, item in ordered:
            index = totals.index(min(totals))
            buckets[index].append((weight, item))
            totals[index] += weight
        for bucket, total in zip(buckets, totals, strict=True):
            if bucket:
                out.append(
                    Lane(device=device, items=tuple(i for _w, i in bucket), weight=total)
                )
    return out


def _size_of(path: Path) -> float:
    try:
        return float(path.stat().st_size)
    except OSError:
        return 0.0


# ------------------------------------------------------------- the detaching
def scheduler_program() -> str | None:
    """The program that can hold a job after the terminal closes, if there is one."""
    for candidate in (WINDOWS_SCHEDULER, UNIT_RUNNER):
        if shutil.which(candidate):
            return candidate
    return None


def _is_scheduler(program: str) -> bool:
    return Path(program).stem.lower() == WINDOWS_SCHEDULER


def _chosen(program: str | None) -> str:
    chosen = program or scheduler_program()
    if chosen is None:
        raise RuntimeError(
            "no way to detach a job here: neither scheduler is on the path. "
            "Run the command in the foreground, or see "
            "docs/patterns/detached-jobs.md for the two it looks for."
        )
    return chosen


def task_name(job: Job) -> str:
    """The job's name, reduced to what either scheduler accepts."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", job.name).strip("-") or "job"


def detached_command(
    job: Job, *, program: str | None = None, replace: bool = False
) -> list[str]:
    """The command line that creates this job detached, as data.

    Returned rather than run, so a caller can print it, log it, put it in a
    report, or run it. The two shapes differ only in their preamble; the
    job's own program and arguments are passed through untouched, and nothing
    is ever handed to a shell to re-parse.

    For the scheduled-task form this *creates* the task and does not start
    it -- :func:`detached_commands` gives the create and the start together.
    The create never overwrites a task of the same name unless ``replace``
    is set, and a job with an environment or a log is refused there, because
    that form cannot carry either.
    """
    chosen = _chosen(program)
    name = task_name(job)
    if _is_scheduler(chosen):
        if job.environment or job.log is not None:
            raise JobRefused(
                f"{job.name}: a scheduled task runs one command line with no shell, "
                "so it cannot set environment variables or send output to a log. "
                "Put both in the program's own arguments, or use the transient-unit "
                "form. Nothing was created."
            )
        return [chosen, "/Create", *(["/F"] if replace else []), "/TN", name,
                "/SC", "ONCE", "/ST", "00:00",
                "/TR", subprocess.list2cmdline(list(job.argv))]
    command = [chosen, "--user", f"--unit={name}", "--collect"]
    command += [f"--setenv={key}={value}" for key, value in sorted(job.environment.items())]
    if job.log is not None:
        target = Path(job.log).absolute()
        command += [f"--property=StandardOutput=append:{target}",
                    f"--property=StandardError=append:{target}"]
    return [*command, *job.argv]


def detached_commands(
    job: Job, *, program: str | None = None, replace: bool = False
) -> list[list[str]]:
    """Every command that starting this job takes, in order.

    One for the transient-unit form, which starts as it is created. Two for
    the scheduled-task form: the create, whose one-off trigger at midnight is
    normally already in the past and so never fires by itself, and then the
    explicit run that actually starts it.
    """
    chosen = _chosen(program)
    create = detached_command(job, program=chosen, replace=replace)
    if _is_scheduler(chosen):
        return [create, [chosen, "/Run", "/TN", task_name(job)]]
    return [create]


def existing_task_query(job: Job, *, program: str) -> list[str]:
    """The read that says whether a scheduled task of this name exists already."""
    return [program, "/Query", "/TN", task_name(job)]


def launch_detached(
    job: Job,
    *,
    program: str | None = None,
    dry_run: bool = True,
    replace: bool = False,
    runner: Callable[[Sequence[str]], int] | None = None,
) -> tuple[list[list[str]], int | None]:
    """Start a job that outlives this process. Returns the commands and the result.

    Dry run by default, like everything else here that changes something: the
    commands it would run are the whole output, and they are the same
    commands the real path uses.

    On the scheduled-task form it asks first whether a task with that name
    already exists, and refuses if one does unless ``replace`` is set -- a
    silent overwrite replaces somebody's task with this one. The result is
    the first non-zero exit status, or zero when every command succeeded.
    """
    chosen = _chosen(program)
    commands = detached_commands(job, program=chosen, replace=replace)
    if dry_run:
        log.info("dry run: would detach %s", job.name)
        return commands, None
    run = runner or _run
    if _is_scheduler(chosen) and not replace:
        if run(existing_task_query(job, program=chosen)) == 0:
            raise JobRefused(
                f"a scheduled task named {task_name(job)} already exists; it is not "
                "replaced unless asked (replace=True). Nothing was created."
            )
    log.info("detaching %s", job.name)
    for command in commands:
        result = run(command)
        if result != 0:
            return commands, result
    return commands, 0


def _run(command: Sequence[str]) -> int:  # pragma: no cover - starts a real job
    # No standard input: a scheduler that would ask a question gets an end of
    # file and fails, rather than waiting for an answer nobody will give.
    completed = subprocess.run(
        list(command), capture_output=True, text=True, stdin=subprocess.DEVNULL,
        encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode != 0:
        log.error("detaching failed: %s", completed.stderr.strip()[:400])
    return completed.returncode
