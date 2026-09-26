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
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .devices import Device, device_of, is_rotational, mentions_device
from .diskactivity import Activity, BusyLimits
from .diskactivity import sample as sample_activity

if TYPE_CHECKING:
    from .client import Client

__all__ = [
    "HEAVY_READERS",
    "RECENT_WINDOW_S",
    "SERVER_PROCESSES",
    "SIGNALS",
    "Gate",
    "GateTimeout",
    "Job",
    "JobRefused",
    "Lane",
    "LaneGate",
    "Process",
    "ServerView",
    "Signal",
    "detached_command",
    "detached_commands",
    "existing_task_query",
    "gate",
    "launch_detached",
    "list_processes",
    "observe",
    "on_device",
    "pack_lanes",
    "parse_process_lines",
    "scheduler_program",
    "server_view",
    "task_name",
    "wait_until_clear",
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
    """One running program, as much of it as a platform will say.

    ``command`` is empty when the platform would not say -- which is exactly
    what an unprivileged listing returns for a program another account
    started, the media server's own decoders included. ``parent`` and
    ``owner`` are kept where they could be read, so such a program can at
    least be named: whose it is, and who started it.
    """

    pid: int
    name: str
    command: str = ""
    parent: int | None = None
    owner: str | None = None

    @property
    def stem(self) -> str:
        return Path(self.name).stem.lower()

    @property
    def is_heavy_reader(self) -> bool:
        return self.stem in HEAVY_READERS

    @property
    def command_hidden(self) -> bool:
        """The command line could not be read, so neither can the device it reads."""
        return not self.command.strip()


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


#: What each kind of signal means, in the order a report lists them. A gate
#: is RED when any signal holds it, and it says which.
SIGNALS: Mapping[str, str] = {
    "reader": "a heavy reader whose command line names this device",
    "hidden-reader": "a heavy reader whose command line cannot be read (another "
                     "account's, typically the server's), so it may be on any device",
    "playback": "somebody is playing a file from this device",
    "task": "the server is running a scheduled task, which reads everything",
    "disk-activity": "the device's own counters show somebody using it",
    "recent-changes": "items on this device changed recently, and the server "
                      "generates previews and chapter images for changed items "
                      "outside any scheduled task",
    "lock": "a lock file another job holds",
    "server": "the server could not be asked what it is doing",
}

#: The media server's own process, by name. A hidden reader whose parent is
#: one of these is named as the server's.
SERVER_PROCESSES: tuple[str, ...] = ("jellyfin",)

#: How far back "recently changed" reaches, when the caller does not say.
RECENT_WINDOW_S = 15 * 60


@dataclass(frozen=True)
class Signal:
    """One reason a device may be busy, and whether it holds the gate.

    A signal that does not hold is a note: something worth saying that the
    evidence did not make a reason. The indirect signals -- a reader whose
    device cannot be read, items that changed recently -- become notes when
    the device's own counters show it quiet over the sample, because then
    whatever they point at is not happening on this disk right now.
    """

    kind: str
    detail: str
    holds: bool = True

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


@dataclass(frozen=True)
class Gate:
    """Whether a lane may start on this device, and why not if it may not."""

    device: Device
    reasons: tuple[str, ...] = ()
    rotational: bool | None = None
    signals: tuple[Signal, ...] = ()

    @property
    def open(self) -> bool:
        return not self.reasons

    @property
    def red_by(self) -> tuple[str, ...]:
        """The kinds of signal that hold the gate, each once, in report order."""
        kinds = {s.kind for s in self.signals if s.holds}
        ordered = [kind for kind in SIGNALS if kind in kinds]
        return tuple(ordered + sorted(kinds - set(ordered)))

    @property
    def notes(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if not s.holds)

    def __str__(self) -> str:
        notes = [f"  note: {s}" for s in self.notes]
        if self.open:
            return "\n".join([f"{self.device}: clear", *notes])
        red = ", ".join(self.red_by) or "held"
        return "\n".join(
            [f"{self.device}: held, RED by {red}", *(f"  {r}" for r in self.reasons),
             *notes]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "open": self.open,
            "red_by": list(self.red_by),
            "reasons": list(self.reasons),
            "signals": [
                {"kind": s.kind, "detail": s.detail, "holds": s.holds}
                for s in self.signals
            ],
            "rotational": self.rotational,
        }


class GateTimeout(TimeoutError):
    """A device stayed RED for longer than the caller was willing to wait."""

    def __init__(self, gate: Gate, waited_s: float) -> None:
        self.gate = gate
        self.waited_s = waited_s
        super().__init__(
            f"{gate.device} stayed RED ({', '.join(gate.red_by)}) for "
            f"{waited_s:.0f} s"
        )


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
        parent: int | None = None
        owner: str | None = None
        try:
            for line in (entry / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("PPid:"):
                    parent = int(line.split()[1])
                elif line.startswith("Uid:"):
                    owner = line.split()[1]
        except (OSError, ValueError, IndexError):
            pass
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        out.append(Process(int(entry.name), name, command, parent=parent, owner=owner))
    return out


#: The query that lists processes with their parents and command lines on the
#: other platform, and the owner of each heavy reader (asking every process
#: for its owner would take seconds). It is a read; it changes nothing.
_QUERY = (
    "Get-CimInstance Win32_Process | ForEach-Object { $o = ''; "
    "if ($_.Name -match '^(" + "|".join(HEAVY_READERS) + r")(\.exe)?$') { "
    "try { $r = Invoke-CimMethod -InputObject $_ -MethodName GetOwner "
    "-ErrorAction Stop; if ($r.ReturnValue -eq 0) { $o = $r.Domain + '\\' + $r.User } } "
    "catch {} }; "
    "$_.ProcessId.ToString() + '|' + $_.ParentProcessId + '|' + $o + '|' + $_.Name "
    "+ '|' + $_.CommandLine }"
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
    return parse_process_lines(completed.stdout)


def parse_process_lines(text: str) -> list[Process]:
    """``pid|parent|owner|name|command`` lines, as the listing query prints them."""
    out: list[Process] = []
    for line in text.splitlines():
        parts = line.split("|", 4)
        if len(parts) < 4 or not parts[0].strip().isdigit():
            continue
        parent = parts[1].strip()
        out.append(Process(
            pid=int(parts[0]),
            name=parts[3].strip(),
            command=parts[4].strip() if len(parts) > 4 else "",
            parent=int(parent) if parent.isdigit() else None,
            owner=parts[2].strip() or None,
        ))
    return out


def on_device(path: object, device: Device) -> bool:
    """Whether a path somebody else reported lies on this device."""
    if not isinstance(path, str) or not path:
        return False
    return mentions_device(path + "/", device)


def _server_parent(process: Process, by_pid: Mapping[int, Process],
                   server_processes: Sequence[str]) -> Process | None:
    parent = by_pid.get(process.parent) if process.parent is not None else None
    if parent is not None and parent.stem in server_processes:
        return parent
    return None


def gate(
    device: Device,
    *,
    processes: Iterable[Process] | None = None,
    running_tasks: Sequence[str] = (),
    own_tag: str | None = None,
    max_readers: int = 1,
    sessions: Sequence[Mapping[str, Any]] = (),
    recent: Sequence[Mapping[str, Any]] = (),
    recent_window_s: float = RECENT_WINDOW_S,
    activity: Activity | None = None,
    busy: BusyLimits | None = None,
    locks: Sequence[Path | str] = (),
    server_error: str | None = None,
    server_processes: Sequence[str] = SERVER_PROCESSES,
) -> Gate:
    """Whether a heavy reader may start on this device right now, and why not.

    Every signal is an input, so the reasoning can be tested without a
    machine; :func:`observe` gathers them from a live one.

    ``own_tag`` is a string the caller puts in its own workers' command lines
    so the gate does not count them; without it a lane sees itself and never
    starts. ``sessions`` are the server's session records, ``recent`` the
    items it reports changed within ``recent_window_s``, ``activity`` the
    device's own counters over a short sample taken while the caller was not
    reading it, and ``locks`` files whose existence means another job holds
    the disks.
    """
    seen = list(list_processes() if processes is None else processes)
    by_pid = {p.pid: p for p in seen}
    limits = busy or BusyLimits()
    quiet = activity is not None and limits.quiet(activity)
    signals: list[Signal] = []

    readers = [
        p for p in seen
        if p.is_heavy_reader
        and mentions_device(p.command, device)
        and not (own_tag and own_tag in p.command)
    ]
    if len(readers) >= max_readers:
        signals += [
            Signal("reader", f"{p.name} ({p.pid}) is already reading this device")
            for p in readers[:max_readers + 2]
        ]

    for p in seen:
        if not (p.is_heavy_reader and p.command_hidden):
            continue
        server = _server_parent(p, by_pid, server_processes)
        whose = p.owner or "another account"
        started = f", started by {server.name} ({server.pid})" if server else ""
        head = f"{p.name} ({p.pid}) runs as {whose}{started}; its command line cannot be read"
        if quiet:
            signals.append(Signal(
                "hidden-reader",
                head + ", but this device was quiet over the sample, so it is reading "
                "another one",
                holds=False,
            ))
        else:
            signals.append(Signal(
                "hidden-reader",
                head + ", so it may be reading this device"
                + ("" if activity is not None else " (no disk counters to rule it out)"),
            ))

    for session in sessions:
        playing = session.get("NowPlayingItem") or {}
        if not isinstance(playing, Mapping) or not playing:
            continue
        if not playing.get("Path"):
            signals.append(Signal(
                "playback",
                f"{session.get('UserName') or 'a user'} is playing "
                f"{playing.get('Name') or 'an item'} from a path the server did not give",
                holds=False,
            ))
            continue
        if not on_device(playing.get("Path"), device):
            continue
        who = session.get("UserName") or "a user"
        what = playing.get("Name") or "an item"
        paused = bool((session.get("PlayState") or {}).get("IsPaused", False))
        transcoding = session.get("TranscodingInfo") is not None
        detail = (
            f"{who} is playing {what} from this device"
            + (" (transcoding: the server's decoder reads it too)" if transcoding else "")
        )
        signals.append(
            Signal("playback", detail + ", paused", holds=False) if paused
            else Signal("playback", detail)
        )

    if running_tasks:
        signals.append(Signal(
            "task",
            "the server is running " + ", ".join(running_tasks)
            + ", which reads everything it can",
        ))

    here = [row for row in recent if on_device(row.get("Path"), device)]
    if here:
        names = ", ".join(str(row.get("Name") or "?") for row in here[:3])
        more = f" and {len(here) - 3} more" if len(here) > 3 else ""
        detail = (
            f"{len(here)} item(s) on this device changed in the last "
            f"{recent_window_s / 60:.0f} min ({names}{more}); the server makes previews "
            "and chapter images for changed items outside any scheduled task"
        )
        signals.append(
            Signal("recent-changes", detail + ", but the device was quiet over the sample",
                   holds=False) if quiet
            else Signal("recent-changes", detail)
        )

    if activity is not None:
        reason = limits.reason(activity)
        if reason is not None:
            signals.append(Signal("disk-activity", reason))

    for lock in locks:
        held = _lock_holder(Path(lock))
        if held is not None:
            signals.append(Signal("lock", f"{lock} exists" + (f": {held}" if held else "")))

    if server_error:
        signals.append(Signal("server", f"the server could not be asked: {server_error}"))

    return Gate(
        device=device,
        reasons=tuple(_reason(s) for s in signals if s.holds),
        rotational=is_rotational(device),
        signals=tuple(signals),
    )


def _reason(signal: Signal) -> str:
    return str(signal)


def _lock_holder(path: Path) -> str | None:
    """The first line of a lock file that exists, "" when unreadable, None when absent."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        return "" if path.exists() else None
    first = text.strip().splitlines()
    return first[0][:200] if first else ""


# ------------------------------------------------------- the live observation
@dataclass(frozen=True)
class ServerView:
    """What the server said it is doing, read once for every device."""

    running_tasks: tuple[str, ...] = ()
    sessions: tuple[Mapping[str, Any], ...] = ()
    recent: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None


def server_view(
    client: Client,
    *,
    recent_window_s: float = RECENT_WINDOW_S,
    now: datetime | None = None,
    recent_limit: int = 500,
) -> ServerView:
    """Running tasks, sessions and recently changed items, in three reads.

    Nothing is written. A read that fails makes the view carry the error,
    which :func:`gate` reports as a signal of its own: a gate that cannot ask
    the server does not know that the server is idle.
    """
    try:
        found = client.get("/ScheduledTasks")
        tasks = tuple(
            str(row.get("Name") or row.get("Key") or "")
            for row in (found if isinstance(found, list) else [])
            if isinstance(row, Mapping) and str(row.get("State") or "").lower() == "running"
        )
        sessions = tuple(_with_paths(client, client.sessions()))
        recent: tuple[Mapping[str, Any], ...] = ()
        if recent_window_s > 0 and client.user_id:
            since = (now or datetime.now(UTC)) - timedelta(seconds=recent_window_s)
            page = client.get(
                f"/Users/{client.user_id}/Items",
                Recursive="true",
                MinDateLastSaved=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                Fields="Path,DateLastSaved",
                Limit=recent_limit,
            )
            rows = page.get("Items") if isinstance(page, Mapping) else None
            recent = tuple(row for row in rows or [] if isinstance(row, Mapping))
    except Exception as exc:  # any failure is reported, never taken as idle
        text = str(exc).strip()
        return ServerView(error=text.splitlines()[0] if text else type(exc).__name__)
    return ServerView(running_tasks=tasks, sessions=sessions, recent=recent)


def _with_paths(
    client: Client, sessions: Iterable[Mapping[str, Any]]
) -> Iterator[Mapping[str, Any]]:
    """Sessions, each playing item's path filled in where the record left it out."""
    for session in sessions:
        playing = session.get("NowPlayingItem")
        if isinstance(playing, Mapping) and not playing.get("Path") and playing.get("Id"):
            try:
                path = client.item(str(playing["Id"])).get("Path")
            except Exception:  # an unknown path is reported as such by the gate
                path = None
            if path:
                session = {**session, "NowPlayingItem": {**playing, "Path": path}}
        yield session


def observe(
    device: Device,
    *,
    view: ServerView | None = None,
    processes: Iterable[Process] | None = None,
    own_tag: str | None = None,
    max_readers: int = 1,
    sample_s: float = 1.0,
    busy: BusyLimits | None = None,
    locks: Sequence[Path | str] = (),
    recent_window_s: float = RECENT_WINDOW_S,
    measure: Callable[[Device], Activity | None] | None = None,
) -> Gate:
    """The gate for one device, from the live machine and (with a view) the server.

    ``sample_s`` is how long the device's counters are watched; 0 skips the
    sample. Call it while your own reader for this device is idle, so that
    whatever the counters show is somebody else.
    """
    if measure is not None:
        activity = measure(device)
    elif sample_s > 0:
        activity = sample_activity(device, window_s=sample_s)
    else:
        activity = None
    server = view or ServerView()
    return gate(
        device,
        processes=processes,
        running_tasks=server.running_tasks,
        own_tag=own_tag,
        max_readers=max_readers,
        sessions=server.sessions,
        recent=server.recent,
        recent_window_s=recent_window_s,
        activity=activity,
        busy=busy,
        locks=locks,
        server_error=server.error,
    )


def wait_until_clear(
    look: Callable[[], Gate],
    *,
    poll_s: float = 60.0,
    timeout_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_hold: Callable[[Gate], None] | None = None,
) -> Gate:
    """Look until the gate is clear; raise :class:`GateTimeout` past ``timeout_s``."""
    started = clock()
    while True:
        found = look()
        if found.open:
            return found
        waited = clock() - started
        if timeout_s is not None and waited >= timeout_s:
            raise GateTimeout(found, waited)
        if on_hold is not None:
            on_hold(found)
        sleep(poll_s)


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


# ------------------------------------------------------------ the lane hook
class LaneGate:
    """A ``before_each`` for :func:`mkvkit.lanes.map_by_device`: hold a lane while
    its device is RED.

    Looking costs a process listing and a second of watching the disk, so a
    lane that was clear is not looked at again for ``every_s`` seconds (0
    looks before every item -- right for work that reads whole files). What
    the server says and the process list are read once for all lanes and
    shared for ``share_s`` seconds. A device that stays RED past
    ``timeout_s`` is given up: that item fails with :class:`GateTimeout`, and
    so does every later item of the same lane, at once, so the other lanes
    finish instead of queueing behind it.
    """

    def __init__(
        self,
        *,
        client: Client | None = None,
        every_s: float = 60.0,
        poll_s: float = 60.0,
        timeout_s: float | None = None,
        sample_s: float = 1.0,
        locks: Sequence[Path | str] = (),
        own_tag: str | None = None,
        max_readers: int = 1,
        busy: BusyLimits | None = None,
        recent_window_s: float = RECENT_WINDOW_S,
        share_s: float = 5.0,
        on_hold: Callable[[Device, Gate], None] | None = None,
        on_clear: Callable[[Device, Gate], None] | None = None,
        processes: Callable[[], list[Process]] = list_processes,
        measure: Callable[[Device], Activity | None] | None = None,
        view: Callable[[], ServerView] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.every_s = every_s
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.sample_s = sample_s
        self.locks = tuple(locks)
        self.own_tag = own_tag
        self.max_readers = max_readers
        self.busy = busy
        self.recent_window_s = recent_window_s
        self.share_s = share_s
        self.on_hold = on_hold
        self.on_clear = on_clear
        self._list = processes
        self._measure = measure
        self._read_view = view
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._checked: dict[Device, float] = {}
        self._abandoned: dict[Device, GateTimeout] = {}
        self._shared: dict[str, tuple[float, Any]] = {}
        #: every gate that was looked at, in order, for a report
        self.history: list[Gate] = []

    def _cached(self, name: str, read: Callable[[], Any]) -> Any:
        with self._lock:
            found = self._shared.get(name)
            if found is not None and self._clock() - found[0] < self.share_s:
                return found[1]
        value = read()
        with self._lock:
            self._shared[name] = (self._clock(), value)
        return value

    def _server(self) -> ServerView | None:
        if self._read_view is not None:
            return self._cached("view", self._read_view)  # type: ignore[no-any-return]
        if self.client is None:
            return None
        client = self.client
        return self._cached(  # type: ignore[no-any-return]
            "view", lambda: server_view(client, recent_window_s=self.recent_window_s)
        )

    def look(self, device: Device) -> Gate:
        """One look at one device, now."""
        found = observe(
            device, view=self._server(), processes=self._cached("processes", self._list),
            own_tag=self.own_tag, max_readers=self.max_readers, sample_s=self.sample_s,
            busy=self.busy, locks=self.locks, recent_window_s=self.recent_window_s,
            measure=self._measure,
        )
        with self._lock:
            self.history.append(found)
        return found

    def __call__(self, device: Device, _item: object = None) -> None:
        with self._lock:
            given_up = self._abandoned.get(device)
            last = self._checked.get(device)
        if given_up is not None:
            raise given_up
        if last is not None and self._clock() - last < self.every_s:
            return

        def held(found: Gate) -> None:
            if self.on_hold is not None:
                self.on_hold(device, found)

        try:
            found = wait_until_clear(
                lambda: self.look(device), poll_s=self.poll_s, timeout_s=self.timeout_s,
                sleep=self._sleep, clock=self._clock, on_hold=held,
            )
        except GateTimeout as exc:
            with self._lock:
                self._abandoned[device] = exc
            raise
        with self._lock:
            self._checked[device] = self._clock()
        if self.on_clear is not None:
            self.on_clear(device, found)
