# Detached jobs

**Implemented by `jfkit.jobs.detached_command()`,
`jfkit.jobs.detached_commands()` and `jfkit.jobs.launch_detached()`.**

A job that reads a whole library takes hours. It must not depend on a terminal
staying open, on a session staying logged in, or on nobody closing a laptop.

A folder of near-identical wrapper scripts once solved this, one per job, each
one a copy of the last with two lines changed. They collapse into one
template with two implementations, and nothing shell-specific survives into
the library.

---

## 1. The command is data

```python
from jfkit.jobs import Job, detached_commands

job = Job(
    name="language scan",
    argv=["mkvkit", "langid", "scan",
          "--jobs", "work/jobs.jsonl", "--out", "work/langid.jsonl"],
    reads=Path("/srv/media/movies"),
)
commands = detached_commands(job)
```

`detached_commands()` returns the argument lists that would start it --
one for the transient unit, two for the scheduled task (create, then run).
`detached_command()` returns the first of them alone. Returned rather than
run, so a caller can print them, log them, put them in a report, review them,
or run them. `launch_detached()` runs them, and is a dry run by default like
everything else here that changes something.

`Job.environment` and `Job.log` are honoured by the transient-unit form: each
variable becomes a `--setenv=`, and the log receives the job's standard output
and standard error, appended. The scheduled-task form runs one command line
with no shell, so it can carry neither, and a job that sets either is refused
there with nothing created -- rather than started without them.

Nothing is ever handed to a shell to re-parse. The job's own program and
arguments are passed through untouched, which is what makes a path with a
space in it a non-event.

## 2. Two hosts, one template

The library looks for whichever of these is on the path:

| platform | program | shape |
|---|---|---|
| Windows | `schtasks` | `/Create /TN <name> /SC ONCE /ST 00:00 /TR <command>`, then `/Run /TN <name>` |
| systemd hosts | `systemd-run` | `--user --unit=<name> --collect [--setenv=K=V ...] [--property=StandardOutput=append:<log> ...] <command>` |

The scheduled task's one-off trigger at midnight is normally already in the
past, so it never fires by itself; the explicit `/Run` is what starts the job.
Before creating it, `launch_detached()` asks (`/Query /TN <name>`) whether a
task of that name exists and **refuses** if one does: `/Create` without `/F`
would stop to ask, and with `/F` it would silently replace somebody's task.
Pass `replace=True` to add `/F` when replacing it is what you mean.

Both are named in full *here*, in the document that explains them, and
assembled from pieces in the source. That is this repository's convention for
host shell command names: they are documentation, and scattering them through
the library is how a cross-platform tool quietly becomes a single-platform
one.

Neither is required. With neither present the call refuses with a message
naming this document, rather than silently running the job in the foreground
and losing it when the terminal closes.

A name with awkward characters in it becomes one the scheduler will accept --
`scan: films & series (2)` becomes `scan-films-series-2` -- rather than
failing at the other end with a message about quoting.

## 3. Write results as you go, one object per line

The log is not a transcript, it is the result. One JSON object per line,
flushed as each unit of work finishes, so that:

- a job interrupted at 80 % has 80 % of its results, not none;
- restarting reads back what is already there and skips it;
- the run can be inspected while it is still running, with ordinary tools;
- a failure is one bad line rather than a corrupt file.

A job that can only be run from the beginning is a job that gets run from the
beginning after every interruption, and long jobs get interrupted.

## 4. Gate on the device, not on the clock

A detached job is exactly the kind that starts at three in the morning and
collides with the server's own background work. Before it starts reading, it
asks:

```
jfkit jobs gate /srv/media/movies --tag lane-a
```

See `docs/patterns/spindle-gate.md`. The one rule worth repeating here: tag
the job's own workers, or the gate sees the reader it just started and waits
for itself.

## 5. The job's own account of itself

Every detached run should end by writing, in its log: what it was asked to do,
what it did, what it refused to do and why, and how long it took. Nobody is
watching when it finishes, and a job whose only output is "exited 0" has told
the next person nothing.

---

## What a wrapper looks like

```
jfkit jobs gate /srv/media/movies --tag langid || exit 1
mkvkit langid jobs /srv/media/movies --out work/jobs.jsonl
mkvkit langid scan --jobs work/jobs.jsonl --out work/langid.jsonl
mkvkit langid report --results work/langid.jsonl --out-dir work/langid
```

`scan` skips every track already in its result file, so running the same line
again after an interruption carries on where it stopped; there is no separate
resume switch.

started with `launch_detached()`, or by hand with the command it prints.

## What is not implemented

- **No supervision.** Nothing restarts a job that died, and nothing notices
  that it did. The scheduler entry is created and forgotten; reading the log
  is how you find out.
- **No dependency between jobs.** Two jobs that must not overlap coordinate
  through the device gate, which is a coarse instrument and the only one
  here.
- **No cleanup of scheduler entries.** The transient-unit form collects
  itself; the task form leaves an entry behind that ran once, which is
  visible and harmless and still somebody's to remove. Until it is removed,
  launching another job under the same name is refused unless
  `replace=True` is passed.
