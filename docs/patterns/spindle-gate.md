# One heavy reader per device

**Implemented by `jfkit.devices` and `jfkit.jobs` (`jfkit jobs gate`,
`jfkit jobs lanes`).**

Mechanical storage serves one sequential reader well and two badly. Not half
as well -- *badly*: the head spends its time travelling between two positions
rather than reading, and a full-file read that took seconds alone can take
minutes in company. A few readers on one disk can make a machine stop
responding altogether.

That is the whole pattern. Everything below is what it takes to make it
actually hold.

---

## 1. A device is a mount point, not a string somebody printed

The first version of this gate compared the strings another program put in its
own output. Those strings carry a protocol prefix on some platforms and not on
others, and they are spelled with whichever separator the program that wrote
them preferred. Paths landed in the wrong lane, two heavy readers ran
on one disk, and the machine was unusable until one of them finished.

    jfkit.devices.device_of(path)   # resolve first, then compare

Resolve the path -- expand, follow links, walk up to the mount point or volume
root -- and compare *that*. A relative path, a link and a path that does not
exist yet all answer with the device a write would land on.

The lesson generalises well past this module: **never key a decision on a
string somebody else formatted.**

## 2. Matching a command line to a device is a shaped test, not a substring one

    jfkit.devices.mentions_device(command, device)

The same failure has a second form. A volume's name appears inside other
programs' arguments -- as a protocol prefix, as a label, as a parameter that
happens to contain the same letter. A substring test on one machine marked
every job on one device as a reader on another.

So the match requires a real path: the volume name followed by a separator,
not preceded by another word character. The test for it is in
`tests/test_jobs.py`, and it is the one worth reading.

## 3. The server is a reader too, and it does not announce itself

After any pass that changes the modification time of a lot of files, a media
server starts regenerating preview tiles and chapter images for every one of
them -- on demand, outside any scheduled task, for hours. From the outside it
is an invisible reader holding the disk.

The gate looks for the decoding and muxing programs by name, on the same
device, whoever started them:

    jfkit.jobs.HEAVY_READERS = ("ffmpeg", "ffprobe", "mkvmerge", "mkvextract")

## 4. Its scheduled tasks count as well

A scan or an analysis pass reads everything it is scoped to. Starting a second
heavy job underneath one is the same mistake by another route, so the gate
takes the list of running tasks and holds if it is not empty.

```
jfkit jobs gate /srv/media/movies
```

Exits zero when the device is clear, non-zero when it is not, and prints the
reason. It is meant to be the first line of a job script.

## 5. A lane must not see itself

    gate(device, own_tag="lane-a")

Give each lane's workers a marker in their own command lines and pass it here.
Without it a lane starts, sees the reader it just started, and waits for
itself forever -- which looks exactly like a gate that is working.

## 6. Unknown storage is treated as needing protection

`jfkit.devices.is_rotational()` answers `True`, `False` or `None`, and `None`
is a real answer: only one platform exposes this without a privileged call.

A gate that assumes "not spinning" because it could not find out is a gate
that lets two readers onto the one disk that could not take them. So the gate does
not decide on that answer at all: every device -- spinning, solid-state or
unknown -- is held at one heavy reader, and the answer is only reported
(`jfkit jobs gate` adds a line when it knows the device spins). Somebody who
knows their storage can take more readers from Python, with
`jfkit.jobs.gate(device, max_readers=N)`; the command line has no flag for it.

## 7. Pack the lanes largest-first

    jfkit.jobs.pack_lanes(items, path_of=..., weight_of=..., lanes=1)

Work is grouped by device, and each device's queue is filled heaviest first.

This is not a detail. A queue worked through in the order it happened to be
built finishes with its biggest item running alone for an hour while every
other lane sits idle. Filled largest-first, the big items start early and the
small ones fill the gaps, and several lanes finish within minutes of each
other.

```
jfkit jobs lanes /srv/media/movies/*.mkv
```

`lanes=2` splits one device into two balanced queues, for storage that can
take it. The default is one, because that is what a spinning disk wants.

## 8. The server's own work hides; ask the disk

The server's decoders run as another account. An unprivileged process listing
returns their names and an **empty** command line, so the device test in
section 2 has nothing to test, and a gate built only on it says "clear" while
the server generates preview tiles on that very disk -- work that a change it
noticed started, and that no scheduled task shows.

So the gate reads more than the process list:

- **a decoder whose command line cannot be read** holds every device
  (`hidden-reader`), named with its owner and, where its parent is the
  server, as the server's;
- **the device's own counters** are watched for a second (`disk-activity`):
  bytes read and written and the time the device was busy, from the volume's
  performance counters on Windows and the kernel's disk statistics on Linux,
  both readable without privilege. Watched while the caller's own reader is
  between items, anything they show is somebody else;
- **the server's sessions** (`playback`), **its running tasks** (`task`) and
  **the items it changed recently** (`recent-changes`), in three reads.

A hidden reader and recent changes are indirect: they say work may be
happening, not where. When the counters show the device quiet, both are
reported as notes and do not hold it. Every other signal holds it outright,
and the gate names each one that did.

Every verb that reads media content waits on this one gate, not on a copy
of part of it: `mkvkit health`, `jfkit dedupe` and `jfkit leftovers` build it
with `jfkit.healthlink.lane_gate` and pass it to `lanes.map_by_device` as
`before_each` (`leftovers` looks once per device before walking it, and
leaves a RED device out instead of waiting).

---

## The shape of a job that uses this

```
for each device:
    wait until `jfkit jobs gate` is clear
    start one worker, tagged so the gate does not count it
    work through that device's lane, heaviest first, appending results as it goes
```

Resumability belongs here too: write results as one object per line as they
are produced, and start by reading back what is already there. A job that can
only be run from the beginning is a job that gets run from the beginning
after every interruption, and long jobs get interrupted.

## What is not implemented

- **No load measurement on other platforms.** The device's own counters are
  read on Windows and Linux only; elsewhere the `disk-activity` signal is
  absent, and a hidden reader holds every device.
- **No cross-machine coordination.** Two machines reading one network share
  are two readers, and nothing here can see the other one.
- **No writer detection by name.** A heavy *writer* is just as bad and is not
  in the list of program names, because the same programs do both and telling
  them apart means parsing their arguments. The device's counters do count
  what is written, so a busy writer shows as `disk-activity`.
