# `mkvkit`: the file side

Tools for looking at a Matroska file, changing one thing in it, and proving
that only that thing changed. Nothing here needs a media server; everything
here reads before it writes, and every command that writes needs to be told
twice.

Install, from a checkout of the repository (`mkvkit` is not on PyPI, and
`pip install mkvkit` from PyPI is not available):

```
git clone https://github.com/lukszi/media-toolkit
cd media-toolkit
pip install -e packages/mkvkit
pip install -e "packages/mkvkit[langid]"    # with spoken-language identification
```

or without a checkout:

```
pip install "mkvkit @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/mkvkit"
```

You will also need [MKVToolNix](https://mkvtoolnix.download/) and
[ffmpeg](https://ffmpeg.org/) on the path. `mkvkit` never bundles them and
never guesses where they are: it looks in the configuration file, then the
environment, then the path, then the places each platform installs things, and
an error tells you every location it tried.

Every example below uses invented names.

---

## The shape of a job

The commands are in the order a job uses them.

```
mkvkit probe    FILE...                       what is in it, by its headers
mkvkit integrity FILE...                      whether the payload is there and plays
mkvkit chapters show|check|rollback|apply     the marks, and the document
mkvkit chapters classify|match|windows        somebody else's names: which job,
mkvkit chapters selfcheck|plan                whether they fit, and what changes
mkvkit tags     show                          the tags, and what they overrule
mkvkit propedit FILE --track UID ...          change a header in place
mkvkit remux    FILE --staging DIR            rebuild it without some tracks
mkvkit verify   ORIGINAL BUILT                prove the difference is the one you asked for
mkvkit swap     KEEPER REPLACEMENT            park the old file, put the new one in its path
mkvkit copy     SOURCE DESTINATION [--move]   copy or move a file, proved on both sides
mkvkit sidecars PATH...                      everything that belongs to a video
mkvkit walk    ROOT                          a tree, without entering links or junctions
mkvkit steps   show|status|apply PLAN        a saved plan, its progress, and a resume
mkvkit langid   jobs|scan|report              identify the spoken language of a track
```

A worked sequence, dropping a dubbed track from a film whose original
language is English:

```
mkvkit probe "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv"
mkvkit remux "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv" \
    --staging /srv/staging --original-language eng --apply
mkvkit verify "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv" \
    /srv/staging/the-quiet-harbour.mkv --dropped 3
mkvkit swap "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv" \
    /srv/staging/the-quiet-harbour.mkv --parked /srv/parked --apply
```

A path with spaces or parentheses in it is quoted, every time: unquoted, the
shell splits it into several arguments, and the parentheses are shell syntax.

`remux --apply` verifies what it just built before it exits: both files are
read in full, one hash per stream, and compared against the rebuild's own plan
(the dropped streams, a moved default, a replaced chapter set). A rebuild that
does not verify exits 1 and stays in staging, and must not be swapped in. It
also prints the equivalent `mkvkit verify` command, with `--dropped`,
`--default-moved` and `--chapters` already filled in, so the check can be
repeated later -- the separate `verify` line above is that repeat.

---

## `mkvkit.integrity` -- is the payload there, and does it play

Everything `probe` says comes from headers, and a header is a promise. A file
whose body was reserved and never written -- a download that stopped, a copy
that did not finish -- keeps a perfect one: the right container, the right
tracks, the right duration, a seek index. Every header-level comparison
passes on it. A duplicate pass that keeps "the better copy" on the strength
of its header can keep the empty file and remove the only one that plays.

```
mkvkit integrity "/srv/media/series/Harbour Lights/Season 02/Harbour Lights - S02E05.mkv"
mkvkit integrity --quick /srv/staging/*.mkv --json work/integrity.jsonl
```

It reads the payload three ways:

1. **a sampled zero-fill read** -- `--blocks` evenly spaced blocks (64 by
   default, 1 MiB each), first byte to last. Compressed media has no
   megabyte-long runs of zeros; a block that is nothing else is space that
   was never written;
2. **a packet scan** -- the demuxer lists every packet of every audio and
   video track (a full read, no decoding), and each track's timeline coverage
   is added up against the container's duration, together with the share of
   the file that is payload at all;
3. **a decode** -- every audio and video track is decoded, and each track's
   decoded duration is added up and what the decoder complains about is
   reported. `--quick` leaves this out.

A track that covers less than 90 % of the container (`--min-coverage`), more
than 2 % of sampled blocks being zeros (`--max-zero-fraction`), less than half
of the file being payload, or any decoder complaint (`--max-decode-errors`)
fails the file. A file that cannot be read, or a missing program, is **no
evidence** -- reported as such, and never as a pass. The exit code is 0 only
when every file passed.

`probe` samples sixteen blocks as well, and reports a file with zero-filled
blocks; where the tracks' stated durations disagree with the container's by
more than a few seconds, it prints each track's duration and a warning. Both
are hints to run `integrity`; neither is the check.

`jfkit delete` runs the full check on the kept copy before it removes another
copy, and both swaps run it on the replacement before they park an original.
There is no switch that skips it.

---

## `mkvkit.health` -- the whole library, cheaply first

`integrity` answers for one file by reading all of it. A library has
thousands, and the ones that matter -- a download that reserved its space and
never filled it, a copy that stopped half way -- look healthy to everything
that reads headers. `health` finds them in two stages, the first cheap enough
to run over everything:

```
mkvkit health /srv/media/movies /srv/media/series \
    --exclude-path "/srv/media/series/Signal Hill" --blocks 8 \
    --json work/health.json --tsv work/health.tsv --titles
```

**Stage 1, every video file, a glance each.** Four checks, none of which
reads more than a few megabytes:

1. a sampled zero-fill read -- `--blocks` evenly spaced blocks (16 by default,
   `--block-kib` each);
2. the container's own extent -- a Matroska segment, the top-level boxes of an
   MP4 or the chunks of an AVI declare how many bytes the file has; a file
   shorter than that was cut off;
3. the probe's durations -- each audio and video track's stated duration
   against the container's (`--duration-tolerance`, or 5 %, whichever is
   more). A single stray packet stamped far past the end makes a twenty-minute
   episode "last" forty-two, and only this comparison shows it;
4. size against duration and bitrate -- the file against its tracks' own
   statistics (or stated bitrates), and against a floor no real picture of its
   size averages below.

A file that passes all four is **OK**, one that fails any is **SUSPECT** with
the evidence, and one that cannot be opened is **UNREADABLE**.

**Stage 2, the suspects only, read whole.** `integrity.check()` -- every packet
listed, every frame decoded -- makes a suspect **CORRUPT**, or clears it to
**OK**. A file the demuxer cannot open at all is CORRUPT too; one nothing
could open, or a missing program, is UNREADABLE. `--confirm N` confirms at
most N suspects, `--no-confirm` stops after stage 1, `--no-decode` lists
packets without decoding.

**One reader per disk.** Both stages run through `lanes.map_by_device`: one
worker per disk, the disks side by side. `--same-disk VOLUME=DISK` puts two
partitions of one disk in one lane. With jfkit installed, each lane waits on
the device gate (`--gate auto`, the default; see `jfkit jobs gate`) before
it starts, and again every `--gate-every` seconds; `--gate local` leaves the
server out, `--gate off` the gate. `--lock PATH` holds every lane while that
file exists, for jobs that take turns across processes.

**Incremental and resumable.** Every answer is appended to `--state` (by
default `health-state.jsonl` in `[paths].work`) as it is known, keyed by path,
size, modification time and a fingerprint of the stage-1 settings. A second
run reads only what changed; an interrupted one continues where it stopped.
A confirmed verdict is kept until the file changes. `--rescan` reads
everything again.

**Bounded runs.** `--time-budget MIN` starts no file after that many minutes;
the rest wait for the next run. `--subset N` reads at most N files per disk,
spread evenly through it, and the summary projects how long every file would
take at the pace measured. A line per disk every `--progress-every` seconds
gives files done, files per second, the read rate and an ETA.

**Output.** The table lists every file that is not OK, worst first, with its
evidence; `--all` adds the rest. `--json` and `--tsv` write every verdict.
`--titles` names each file that is not OK by the server item that plays it
(through `jfkit.query`). Confirmed corrupt files are printed as a `jfkit
delete` manifest (`--manifest PATH` writes it) under the category `corrupt`,
which `jfkit delete` does not check yet: the manifest is for a person to
release. The exit code is 0 only when every file is OK and every file was
reached.

**Read-only, always.** Nothing is moved, renamed or deleted.

## `mkvkit.transfer` -- copy or move, and prove the copy

A move between volumes is a copy followed by a delete, and only the delete
cannot be undone. `mkvkit copy` does it in the one order that is safe:

```
mkvkit copy "/srv/one/Northwind/Featurettes/Making Northwind.mkv" \
    "/srv/two/Northwind/Featurettes" --stage /srv/two/staging --move --apply
```

1. an existing destination is refused, never replaced;
2. the file is copied to a partial file -- in `--stage`, a folder on the
   destination's volume outside anything a media server watches, or beside
   the destination -- while the source is hashed, and flushed to the device;
3. the partial file is hashed again, from the destination side, and has to
   match;
4. it is renamed into place, without replacing anything that appeared in the
   meantime, so nothing ever sees a half-written file under its real name;
5. only then, with `--move`, is the source removed.

A failure at any step removes the partial file and leaves the source as it
was. The library side is `mkvkit.transfer.verified_copy()`.

**The cache.** The read-back in step 3 comes straight after the write, and
the operating system usually answers it from memory: it proves what was
handed to the device, not what the device kept. On a disk you have reason to
doubt, read the file again later -- `mkvkit integrity`, or a second hash --
before removing anything else that depends on it. Every report says so.

---

## `mkvkit.sidecars` -- everything that belongs to a video

A video in a library is rarely one file. Rename, move or park it alone and
its metadata, pictures, subtitles and preview tiles stay behind, attached to
nothing. `mkvkit sidecars` lists what has to go with it, and reads nothing
but directory listings:

```
mkvkit sidecars "/srv/media/series/Northwind/Northwind - S01E03 - The Quiet Harbour.mkv"
mkvkit sidecars /srv/media/series/Northwind --json
```

A file belongs to a video when its name starts with the video's name without
the extension (the *stem*, compared without regard to case) followed by `.`
or `-`. What follows the stem is kept by a rename. Each sidecar has a kind:

| kind | what | read by the server |
|---|---|---|
| `trickplay` | the `<stem>.trickplay` folder of preview tiles | yes |
| `nfo` | `<stem>.nfo` | yes |
| `image` | `<stem>.jpg`, `<stem>-thumb.jpg`, `-poster`, `-fanart`, ... in any picture format, and `metadata/<stem>.jpg` | the names the image providers know |
| `subtitle` | `.srt`, `.ass`, `.ssa`, `.vtt`, `.sup`, `.sub`/`.idx`, ... | only as `<stem>.<flags>.<ext>` -- the dot is the only flag delimiter |
| `audio` | `.mka`, `.ac3`, `.dts`, ... | same rule as subtitles |
| `chapters` | `<stem>...xml` | no |
| `other` | anything else named after the video: `.txt`, `.ttml`, `.edl` | no |

Another video is never a sidecar, and a file belongs to exactly one video:
the one with the longest stem that claims it, so
`Harbour.Lights.S01E02.German.DL.srt` goes with
`Harbour.Lights.S01E02.German.DL.mkv` and not with
`Harbour.Lights.S01E02.mkv`. A folder listing also reports the files that
belong to no video (`unclaimed`).

The library side: `sidecars_of(video, listing=None) -> SidecarSet`,
`sidecars_in_folder(folder) -> FolderSidecars`, `planned_renames(video,
new_video)` (the video first, then every sidecar, each with its target), and
`FolderListing.read(folder)` to list a folder once for many videos.

---

## `mkvkit.walk` -- a tree, without wandering off it

On Windows a junction is a directory that `os.path.islink` says is not a
link, so a plain walk descends into it and an inventory of one disk quietly
becomes an inventory of another. `mkvkit.walk.walk()` does not enter a
symbolic link, a junction or any other directory reparse point, and reports
each one it left out, with anything excluded and any folder it could not
list:

```
mkvkit walk /srv/media/series --exclude Extras --exclude "*.partial" --json
```

`walk(root, exclude=(), exclude_paths=(), follow_links=False,
include_dirs=False, suffixes=None, sizes=True, on_skip=None)` returns a lazy
`Walk`: iterate it for `Entry(path, relative, is_dir, size, link)`, then read
`walk.skipped`, a list of `Skipped(path, reason, detail)` with the reasons
`symlink`, `junction`, `reparse-point`, `excluded`, `loop` and `error`.
Patterns match an entry's name or its `/`-separated path relative to the root.
With `follow_links=True` every directory is entered once, by device and
inode, so a loop ends. `is_link_or_junction(path)` is the single check.

It lists each directory once with `os.scandir`. On Windows the listing
already carries attributes, reparse tag and size, so it needs no further
calls. Elsewhere a size costs one `lstat` per file, which `sizes=False`
saves.

---

## `mkvkit.steps` -- plan, dry run, apply, audit, resume

A verb that changes many things -- a rename that carries a dozen sidecars, a
replay of play state onto a hundred new items -- goes through one shape:

1. **a plan**: `Plan(verb, steps)`, an ordered list of `Step(id, action,
   params, summary)` with JSON parameters, built before anything happens;
2. **the dry run**: `plan.render()` prints it and `plan.save(path)` writes it
   for review. It is the plan itself, not a simulation of it;
3. **apply**: `apply(plan, actions, audit=path)` runs each step through the
   action its name selects, and appends one JSON line per event to the audit
   (`plan`, `start`, `done`, `failed`, `skipped`), flushed as it goes;
4. **resume**: run the same plan with the same audit again. Steps the audit
   records as done are skipped, and a step whose action can tell its change is
   already in place (`Action.done`) is recorded as done without running. A
   failure stops the run and leaves the rest pending; `attempts=N` retries a
   refused access with a doubling back-off.

The plan's fingerprint -- a digest of its verb and steps -- ties an audit to
one plan, and `load_plan()` refuses a saved plan whose steps were edited after
it was saved. `FILE_ACTIONS` holds `mkdir`, `rename` (one volume, never
replaces anything, folders too) and `copy`/`move` (through
`transfer.verified_copy`); a verb adds its own actions for anything else.
`add_plan_arguments(parser)` gives a verb `--plan-out`, `--plan` and
`--audit`.

```
mkvkit steps show plan.json
mkvkit steps status plan.json --audit rename.audit.jsonl
mkvkit steps apply plan.json --audit rename.audit.jsonl --apply
```

`steps apply` runs a saved plan made only of file steps, and is how a
rename that stopped half way -- a file held open, an access refused -- is
finished: the same command again, after the cause is gone.

---

## `mkvkit.lanes` -- one reader per disk, bounded requests elsewhere

Two kinds of fan-out, kept apart on purpose:

- `map_bounded(work, items, workers=None)` runs requests side by side, at
  most `workers` at a time (default `DEFAULT_WORKERS` = 4, capped at
  `MAX_WORKERS` = 32). Each item's result or exception comes back in an
  `Outcome`, in the order of the items.
- `map_by_device(work, items, path_of=..., device_of=device_of,
  weight_of=None, max_devices=None, before_each=None)` is the only way to fan
  out work that reads file content. Items are grouped by the device behind
  their path, every device gets exactly **one** worker, and the devices run
  side by side. Nothing puts a second reader on one device. Heaviest first
  within a device when `weight_of` is given; `before_each(device, item)` is
  where a gate that waits for a quiet disk goes.

A device is a volume (`mkvkit.devices.device_of`, which `jfkit.devices`
re-exports). Two partitions of one disk are two volumes: pass a `device_of`
that maps them to one name when that is the case.

---

## `mkvkit.probe` -- read the file once, with both programs

Two programs can describe a media file and they describe different things.
The muxer's identification output knows the container: track identifiers, the
unique identifier each track carries, the flags, the attachments, the chapter
editions. The probe knows what a player will make of it: the stream order, the
dispositions, and the metadata a demuxer resolves -- which is not always what
the container's header says.

```python
from mkvkit.probe import probe, container_mismatch

found = probe("/srv/media/movies/example.mkv")
for track in found.audio:
    print(track.id, track.uid, track.language, track.effective_language)
print(found.has_cues, found.language_disagreements)
```

Three things it handles so nothing else has to:

- **The extension is not the container.** `container_mismatch()` is the guard
  every writing path calls first; the header editor does nothing at all to a
  file that is not really Matroska, and exits zero while doing it.
- **The two programs spell some language codes differently.** Everything that
  leaves this module is canonicalised, with the raw spelling kept beside it.
- **A tag can overrule the track header.** Both programs will tell you, and
  `language_disagreements` collects the answer, so an edit that would change
  nothing anybody sees can be refused before it runs.

`probe_from_json()` builds the same object from recorded output, which is how
the parsing is tested without either program installed.

## `mkvkit.chapters` -- the marks, and the document that carries them

`chapters.xml` reads and writes the document; `chapters.grid` compares two sets
of marks.

```python
from mkvkit.chapters.xml import read_chapters, build, rollback, selfcheck

marks = read_chapters("/srv/media/movies/example.mkv")
for problem in selfcheck(marks, runtime_s=5400.0):
    print(problem)                       # "reject: ..." blocks; "note: ..." does not
open("rollback.xml", "w").write(rollback(marks))
```

`mkvkit chapters rollback FILE --out DOC` and `mkvkit chapters match ... --out
DOC` never replace an existing `DOC`; pass `--force` to do that on purpose.

Applying a document **replaces** the element: a document with two marks where
the file has sixteen deletes fourteen. So `selfcheck()` is not optional, and
it blocks an empty document, marks that do not increase, two marks on one
timestamp, a hidden mark, a last mark past the runtime, a count that does not
match the file, a name carrying a replacement character, and a document whose
names are all labels.

The reader models one edition of top-level marks with one name each. A file
or document with marks nested under a mark, or a mark named in more than one
language, is read (so it can be shown and compared) but never written back
from that model: `build()` and `rollback()` raise, and `chapters apply`,
`chapters rollback` and `remux --chapters` refuse, because the write would
delete what the model does not carry.

**A mark with no name has no display block.** Writing `Chapter 7` into a file
looks the same in a player and is not reversible by inspection, because
nothing downstream can tell a name somebody chose from a label a program made
up. `downstream_label()` gives the label a player shows, for the one place it
is needed: comparing what you wrote against what a consumer displays.

`chapters.grid` answers "does this published list describe my cut?", at three
scales -- as written, rate-converted, and the inverse -- with the median
offset removed before the deviations are scored. Within two seconds is a
match. A matched candidate's **names** may be copied onto your own marks
(`copy_names`), which never moves a mark; its **marks** may be written into a
file that has none only if no rate conversion was needed (`adopt`), because
the grid agreement was the only evidence that it is the same cut.

A matching grid proves the marks fit. It says nothing about whether the names
were typed against them -- which is the next section.

## `mkvkit.chapters` -- somebody else's names, and your own

A published chapter list raises one question the arithmetic cannot answer, and
it turns out to matter: a grid can match to under two seconds while half of
the names describe a scene a couple of chapters away. The method document is `docs/methods/chapter-names.md`; this is the
shape of it in code.

```python
from mkvkit.chapters import names, sources, verify

job = sources.classify(marks, candidate, runtime_s=5400.0)
print(job)                       # "names-only: the grids agree ..."

evidence = verify.collect_evidence(path, marks, provider=provider)
result = names.match_names(marks, candidate, evidence=evidence)
if result.accepted:
    document = result.chapters   # the file's own marks, carrying the names
```

`sources` names the three jobs a candidate can be, and they carry very
different risk: **names onto marks you already have** (safe -- the worst case
is a wrong name on a mark that has not moved), **times and names onto a file
with none** (only a runtime stands behind it, so it asks for a person), and
**times only** (parked). It also carries one worked adapter for an archive of
the kind that exists: a search returning several candidates per title and a
document per candidate, with the fetching injected so a test passes a
dictionary. It is **disabled unless you enable it**, cached on disk,
rate-limited and identified.

`verify` is the half that needs the content. Twenty seconds of
original-language audio from each mark and one frame shortly after it, both
out of a *single* seek; the content-word hit rate of the name list scored at
offsets −4 to +4, where a clearly better score away from zero is a list that
was typed against different marks; and corroboration from any second published
list you hold. Only an `ALIGNED` verdict is eligible to be written, and a list
that fits better elsewhere is **refused rather than slid into place**.

`names` holds the matcher and the rules -- length, punctuation, chapter
numbers, timecodes, language, and the mechanical answer for a chapter with no
dialogue, which is the structural name where one belongs and the empty string
everywhere else. `windows` cuts a transcript into one window per mark, sized
by the chapter's duration and sampled evenly across it. `selfcheck` grades
names that were written rather than sourced, and one `wrong` grade holds the
whole film back.

There is **no namer** here and there is not going to be one: the writing step
in the work this came from was a person reading the windows. What ships is the
window builder, the rules, the self-check and the verifier.

## `mkvkit.plan` -- one file, everything that would change

```python
from mkvkit import plan

job = plan.chapter_names_plan(path, result, source="an example chapter archive")
print(job)                                  # this is what --dry-run prints
outcome = plan.apply(job, dry_run=False)    # chapters and tags in one invocation
```

Three properties earn it an object. Every pending change to a file goes in
**one** invocation, because two edits are two modification times and a media
server regenerates an item's preview tiles and chapter images on each one. A
**refusal is part of the plan**, carrying its reason, rather than an exception
that ends a pass. And `explain_churn()` compares two runs of the same pass and
says which files entered, which left and which changed -- without which there
is no way to tell a policy change from a bug, because both look like a
different list.

## `mkvkit.tags` -- the element that overrules the header

```python
from mkvkit.tags import read_tags, set_track_language, provenance, merge, build

tags = read_tags("/srv/media/movies/example.mkv")
tags = set_track_language(tags, uid, "deu")
tags = merge(tags, [provenance("chapter names", "an example source")])
```

Three rules, enforced rather than remembered:

- a language change is two edits (header and tag) or it is not a language
  change;
- writing tags replaces every tag in the file, so the only safe write is one
  built by merging into what is already there;
- compare tags as `(track identifiers, name, value)` triples -- the editor
  re-emits the target block in its own normal form, so anything finer reports
  every tag as lost and re-added on every run. A nested tag is named by its
  path (`ARTIST/SORT_WITH`) and a binary value by its text, so a nesting that
  came back flattened or a binary value that came back empty is a loss.

A read keeps what a simple tag can carry -- nested simple tags, a binary
value and its format, both language elements and the default flag -- and
`build()` writes all of it back. A document holding any element outside that
set is read, but `build()` refuses it (`TagError`), so it is never written
back with that element missing.

`provenance()` writes where something came from into the file itself. Merging
the same provenance twice leaves one.

## `mkvkit.propedit` -- one edit in place, and proof it changed no more

```python
from mkvkit.propedit import TrackEdit, safe_propedit

result = safe_propedit(path, [TrackEdit(uid, language="deu")], dry_run=False)
print(result.ok, result.problems)
print(result.rollback.to_tsv())
```

Tracks are named by the identifier they carry, not by position among their
type. A header language change is refused outright when a tag would go on
overruling it. Afterwards the file is read again and compared track by track,
keyed on the identifier: everything asked for happened, nothing else moved,
the chapter count only moved if a document was written, and the tags survived.

The rollback is captured before the edit whether or not anything is applied --
the previous value of every property the edit touches, plus the chapter and
tag documents the file had, exactly as the extractor printed them. It cannot
be reconstructed afterwards.

On an applied edit it is **written to disk before the editor runs**, and an
edit whose rollback cannot be written does not happen. `mkvkit propedit`,
`mkvkit chapters apply` and `mkvkit chapters plan` take `--rollback-dir DIR`;
without it the directory is `<[paths].work>/rollback` (`./work/rollback` with
the default configuration). Each applied edit leaves, named after the file and
the time to the microsecond, and never overwriting an earlier one:

- `NAME.TIME.rollback.tsv` -- `path`, `track_uid`, `property`,
  `previous_value`, one row per property the edit touched (an empty previous
  value means the file had none), plus a row naming the saved chapter or tag
  document, or `(none)` when the file had none;
- `NAME.TIME.chapters.xml` -- the chapter document the file had, when marks
  were written;
- `NAME.TIME.tags.xml` -- the tag document the file had, when tags were written.

To put a file back, hand those to the header editor: per TSV row
`mkvpropedit FILE --edit track:=UID --set PROPERTY=VALUE` (or `--delete
PROPERTY` where the previous value is empty), `--chapters NAME.TIME.chapters.xml`
and `--tags all:NAME.TIME.tags.xml` (an empty `--chapters ""` or `--tags all:`
where the file had none). `Rollback.restore_command(files)` builds that
argument list; the saved documents are the extractor's own output and are
applied with the editor directly, not re-read through this package's model.

## `mkvkit.verify` -- prove the difference is the one you declared

```python
from mkvkit.verify import collect, compare, TracksDropped

result = compare(collect(original), collect(built), TracksDropped(dropped={3}))
print(result)          # PASS/FAIL, the problems, and the notes with their reasons
```

The primary evidence is one hash per stream, taken with a stream copy on both
sides. The expected difference is an argument -- `TracksDropped`,
`TracksAppended`, `HeaderOnly` -- so the question is not "are these the same?"
but "is the difference the one that was intended?".

`TracksDropped(default_moved=True)` (`--default-moved`) lets the default flag
change and then requires exactly one audio track to carry it.
`TracksDropped(replaced_chapters=...)` (`--chapters DOCUMENT`) holds the new
file's marks to the document that was written in, instead of to the
original's, and fails a file that ended up with more than one edition.

Some differences are notes, with their reasons: a modern language subtag
appearing while the legacy element is unchanged; identifiers the muxer
regenerated; a container that got *shorter* while every kept hash matched.
Each keeps its teeth -- two different modern subtags still fail, and a
duration that grew still fails. A missing seek index is a failure. Audio
timing is compared to forty milliseconds.

After a header edit, `reheader()` refreshes the header half of the evidence
and keeps the hashes: a header editor cannot touch a payload, so re-reading
both files would measure something that provably did not change.

## `mkvkit.remux` -- decide, then build somewhere else

```python
from mkvkit.remux import RemuxPolicy, plan, build

policy = RemuxPolicy.from_config(config)
rebuild = plan(found, policy, output=staging / found.path.name,
               original_language="eng")
print(rebuild)                      # every track, and why it lives or dies
build(rebuild, dry_run=False)
```

Nothing is dropped that a policy did not name; an unknown language and an
untagged track are both kept. The original language is never dropped, and the
planner takes it as an argument rather than guessing. A commentary track is
never dropped for its language. A plan that would leave nothing a policy
language or the original language names is refused -- that is the guard that
catches a plan built on a wrong original language.

Two flags travel together: a chapter document is always passed with the
option that suppresses the file's own marks, or the file ends up with both
sets. And a source with no modern language subtags is muxed with them turned
off, so a track-dropping operation does not quietly rewrite headers. Moving
the default audio track sets the flag on the new default **and clears it on
every other kept audio track**, so the file never carries two.

`build()` refuses, rather than replaces, anything already at the output path,
its `.part` file or its chapter document in staging. The part file and the
chapter document are removed whether the muxer succeeds or fails.

The output is never the input. `plan.expected_delta()` hands the verification
its own declaration of what changed, so the two cannot drift apart.

## `mkvkit.swap` -- park the original, put the rebuild in its path

```python
from mkvkit.swap import SwapPair, swap

swap(SwapPair(keeper, replacement), parked_dir="/srv/parked", dry_run=False)
```

The original is **moved**, never deleted, into a parking directory that keeps
its own layout. The rebuild takes the old file's exact path, because
everything downstream is keyed on that path. What arrived is read again in
place -- never compared by size, since a rebuilt file legitimately has a
different size. Any failure puts the original back before the error is
reported.

## `mkvkit.langid` -- what language is actually spoken

Shipped at 0.1.0 and documented in the changelog: sampling windows by where
the speech is, a settle ladder with an asymmetric bar, context priors, and a
report that leads with what did not settle.

---

## Configuration

One TOML file, found at `--config PATH`, `$MEDIATOOLKIT_CONFIG`,
`./mediatoolkit.toml`, or the per-user location. A secret never has a value in
it.

```toml
[tools]
mkvmerge = "mkvmerge"

[paths]
staging = "/srv/staging"
parked  = "/srv/parked"

[policy]
keep_languages      = ["eng", "deu"]
droppable_languages = []          # opt-in, and empty by default
default_audio       = "eng"

[langid]
model_dir = "/srv/models"         # the speech model's cache; otherwise the library's own
```

`[server].url` is an address only: one carrying a user name or password
(`http://user:pass@example.com`) is refused, because the address is printed in every
log line about the server. The token goes through `token_env` or
`token_command`.

`droppable_languages` is empty by default and that is deliberate: a toolkit
that removes tracks from somebody's files because a list was left blank has
chosen the wrong default.

## The rules this package will not be talked out of

- **Dry run is the default.** Every writing command needs `--apply`.
- **The output is never the input.** A rebuild is staged, verified, swapped.
- **The original is parked, not deleted.**
- **"It exited zero" is not verification.** Nothing here reports success
  because a command did.
- **A confidently wrong value is worse than no value.** Where the evidence
  does not settle, these tools do nothing and say so.

See `docs/methods/verification-discipline.md` for the reasoning,
`docs/methods/chapter-names.md` for the chapter-name method in full, and
`docs/gotchas/matroska-ffmpeg.md` for the findings each rule came from.
