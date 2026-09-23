# media-toolkit

Three small tools for keeping a self-hosted video library correct, in one
repository.

| Package | Install | What it does |
|---|---|---|
| `mkvkit` | `pip install mkvkit` | the file side: safe header edits, original-vs-rebuilt verification, chapters, Matroska tags, container probing, spoken-language identification |
| `jfkit` | `pip install jfkit` | the server side: API client, whole-record round-trip, non-replacing refresh, filename-parse prediction, library options, database and preview upkeep, surveys, in-place swapping, evidence-first deletion, segment scoping, detached jobs |
| `dubalign` | `pip install dubalign` | aligning a foreign-language dub to a different transfer of the same title: decode, measure, find the seams, splice, verify |

`mkvkit` and `dubalign` are useful with no media server at all. `jfkit` talks
to Jellyfin 12.x over HTTP; everything here was validated against 12.1.

This project is not affiliated with, endorsed by, or part of the Jellyfin
project.

## Install

```
pip install mkvkit
pip install "mkvkit[langid]"      # spoken-language identification
pip install "dubalign[align]"     # changepoint detection and resampling
```

Python 3.11 or newer. The external programs (ffmpeg, ffprobe, and the
MKVToolNix command-line tools) are discovered at run time and never vendored;
speech-model weights are never vendored either -- the model name and its cache
directory are configuration.

From a checkout:

```
pip install -e packages/mkvkit -e packages/jfkit -e packages/dubalign
```

## Configuration

One TOML file replaces every hardcoded path, identifier and token. It is found
in this order:

```
--config PATH  ->  $MEDIATOOLKIT_CONFIG  ->  ./mediatoolkit.toml  ->  per-user
```

The per-user location is the platform's own: the roaming application-data
directory on Windows, `$XDG_CONFIG_HOME` (or `~/.config`) elsewhere, under
`media-toolkit/config.toml`.

```toml
[server]
url       = "http://127.0.0.1:8096"
token_env = "JFKIT_TOKEN"          # the token itself is never in this file
user_id   = "00000000-0000-0000-0000-000000000000"

[tools]
ffmpeg   = "ffmpeg"                 # a name on PATH, or an absolute path
mkvmerge = "mkvmerge"

[paths]
movies  = "/srv/media/movies"
staging = "/srv/staging"            # where rebuilds are assembled
parked  = "/srv/parked"             # where replaced originals go, and stay
work    = "./work"                  # logs, caches, evidence

[policy]
keep_languages = ["eng", "deu"]     # never dropped
default_audio  = "eng"
```

A full annotated file is in `examples/mediatoolkit.example.toml`. Every value
in it is invented.

Secrets are never written into it. The file names an environment variable
(`token_env`) or a command that prints the secret (`token_command`, e.g. a
password-manager invocation). A literal `token = "..."` is refused at load
time, with an explanation rather than a stack trace. The loaded configuration
object is passed explicitly to everything that needs it: no module-level
singleton, nothing that hands a caller a token by being imported.

Validation reports *every* problem in one message, not the first one it hits.

## What is released

`mkvkit` 0.3.0, `jfkit` 0.3.0 and `dubalign` 0.1.0.

**Edit a Matroska file, and prove you changed only what you meant to.**
`mkvkit probe` says what is in a file, from both programs that can describe
one. `mkvkit propedit` changes a track header in place, selecting the track by
the identifier it carries, and reads the file back to prove nothing else
moved. `mkvkit remux` rebuilds a file without the tracks a policy allows
dropping -- into a staging directory, never over the input -- and
`mkvkit verify` compares the two files against a *declared* difference, with
one payload hash per stream as the evidence. `mkvkit swap` then parks the
original and puts the rebuild in its exact path. `mkvkit chapters` and
`mkvkit tags` read, check and write the two elements that are easiest to
damage by accident. Everything that writes defaults to a dry run.

**And a chapter list that matches your marks may still describe another
film's scenes.** A published list whose timestamps agree with your file's to
within two seconds is certainly for your cut; that is no evidence at all about
its *names*, and a list can match that well while half of its names describe
a scene a couple of chapters away. `mkvkit chapters classify|match|windows|selfcheck|plan` is the
method as code -- which of three jobs a candidate is, whether the names sit on
the marks they describe, and a refusal rather than a guess where they do not.
`docs/methods/chapter-names.md` is the write-up.

The reasoning behind all of it is `docs/methods/verification-discipline.md`,
and every rule in that document names the function that implements it.

Two more things work end to end, and both of them read rather than write:

**Identify the spoken language of the audio tracks in a library.**
`mkvkit langid jobs` lists the tracks, `scan` reads them and records what a
detector heard per window, and `report` decides and says what is left. The
deciding is pure arithmetic over stored probability vectors, so it can be
re-run against evidence collected months ago when a threshold changes -- and
it runs with nothing installed.

**Put an audio track from one transfer onto a different transfer of the same
programme.** `dubalign map` measures the offset across the whole runtime rather
than at one point, because one point cannot tell a constant offset from a step
from a rate difference. `dubalign changepoints` finds the jumps instead of
having them typed in, and pins each one down to a few tens of milliseconds.
`dubalign plan` writes a document you can read, diff and edit, with the joins
placed on the jumps and scored on the span each one actually repeats or drops.
`dubalign splice` builds it, `dubalign encode` writes it, and `dubalign verify`
scans the whole result against the keeper and holds every window to 40 ms --
every window, not the average. `dubalign controls` runs four questions whose
answers were written down first, and nothing else is believed until it passes.

**Administer a media server without emptying the fields you did not
mention.** `jfkit survey` answers six questions about a collection --
completeness, audio languages per track, chapter state, containers,
duplicates, filename parsing -- in one shape, in five formats, with the scope
and the caveats inside the document. `jfkit item set` round-trips a whole
record and writes the phases in the one order that survives a refresh.
`jfkit refresh` asks for a non-replacing refresh, waits for the queue rather
than reading once and believing it, and reports everything that moved outside
what was expected. `jfkit swap` puts a rebuilt file in an item's exact path so
its identity, its play state and its name survive, with the service stopped
per chunk and the chunks sized in bytes. `jfkit delete` refuses to touch
anything whose category nobody released, checks every precondition against the
world rather than against the manifest, and parks rather than deletes.
`jfkit libopts`, `jfkit maintenance`, `jfkit segments` and `jfkit jobs` cover
library options, the catalogue database, scoping a segment pass, and not
putting two heavy readers on one disk.

**Predict how a media server will read a filename, before renaming.**
`jfkit naming PATH...` prints what each name would be read as and exits
non-zero if any of them would be read as an episode *range*, which is the
expensive mistake. It is pinned by a table of sixty-five invented names and it
records the release it was checked against.

Beside them, references that stand on their own: `docs/gotchas/` on one media
server release and on Matroska and ffmpeg, guides to each side in
`docs/mkvkit.md` and `docs/jfkit.md`, `docs/methods/` on the verification
discipline, the spoken-language settle ladder, aligning a dub to a different
transfer, the swap procedure and evidence-first deletion, `docs/patterns/` on detached jobs and the device
gate, `docs/runbooks/` on looking after the catalogue database, and two fully
invented worked recipes under `examples/recipes/`. Every gotcha entry carries the symptom,
the cause and the fix; each reference states the versions its entries were
confirmed against, and an entry that names how it was confirmed is one that
was reproduced rather than met in passing; every method document names the function that implements each of its
rules and ends with what is *not* implemented, or says in its first paragraph
that it implements none.

**And one document about running the work rather than about using a tool.**
`docs/operating-playbook.md` is how a long cleanup is run safely when
most of the mechanical labour is handed to automation: one document that *is*
the state, and the discipline of refreshing it; a numbered ledger of
everything that surprised you, each entry pinned to a version; releasing a
whole category before anything in it may be touched; the dry run as a plan you
review and diff; staging elsewhere and swapping in place so identifiers
survive; verifying both sides inside the final container; one heavy reader per
device; detached, resumable jobs; one kept log per pass; what a person decides
and what automation decides; and how to hand the job to a reader with no
context. It is written for operators rather than for developers, and none of
it needs this repository installed.

Beside it, `docs/methods/handoff-audit.md`: how to find out whether that state
document is still true. Independent readers are pointed at the running system
and told to *refute* its claims rather than to review them, contested claims
are arbitrated and judged on a rubric of three verdicts and three severities,
and every one of them leaves with a corrected sentence. It is the only method
document here with no implementing function, and it says so in its first
paragraph.

**Nothing in the tree is a skeleton any more.** Every module that was one at
the first release of either package is implemented, tested and documented at
`jfkit` 0.3.0 and `mkvkit` 0.3.0. Each package's `CHANGELOG.md` says what
arrived when, and what each release deliberately does not do -- the largest of
which is that there is no chapter *namer* here, only the machinery that
checks names and refuses the ones that do not hold up.

## Safety principles

These four are the reason the project exists in this shape.

**Dry run first.** Every command that changes a file or a server record
defaults to a dry run and requires an explicit `--apply`. A command with no
dry-run path is not merged.

**Park, never delete.** Nothing is removed. A replaced original is *moved* to
the configured parked directory and left there; the caller decides, later and
by hand, whether it goes. Deletion tooling produces evidence and a manifest --
it does not free space on its own.

**Verify both sides, inside the final container.** "The command exited 0" is
not verification. A rebuild is proved by collecting stream-level evidence from
the original and from the result -- track table, per-stream payload hashes,
chapters, container-level fields -- and comparing them against a declared
expected difference. Anything outside that difference is a failure. Cosmetic
differences are recorded with the reason they are cosmetic, never silently
allowed, and measurement happens in the rendered output, never in an
intermediate.

**Known-answer controls before trusting a measurement.** A measurement
pipeline is run against pairs whose answers are already known -- a track
against itself must read zero, a deliberately shifted pair must return the
shift it was given, the same pair measured at two starting points must agree,
and moving the *data* by a known amount must move the answer -- before any of
its numbers are believed. A measurement that has never been given a question
with a known answer is a number generator that has not yet been caught.

## About the numbers

Several constants here were **fitted** against one collection: the
language-identification confidence bars, the agreement fractions, the
conditions on the trimmed aggregate, and the chapter window budgets. They ship
as documented defaults with the method used to fit them, so they can be
re-fitted on your own material -- `docs/methods/langid-ladder.md` gives the
re-fitting procedure.

The alignment tolerance of 40 ms is a different kind of number, and it is
labelled as one: a **chosen** house bar of roughly one frame of picture, not a
value fitted against anything. `docs/methods/pal-alignment.md` says so where it
defines it.

Neither kind is a universal, and nothing in the documentation claims they
are.

Nothing in the documentation describes the material they were fitted on: no
counts, sizes or timings of anybody's collection.

## Status

Published as-is while it is being built, under the PolyForm Noncommercial
License 1.0.0 (`LICENSE`): free to use, change and share for any noncommercial
purpose, which is not the same thing as an open-source licence.
`CONTRIBUTING.md` has the commit convention, the privacy rule every change is
checked against, and the recipe for splitting a package out into its own
repository.
