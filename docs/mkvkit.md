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
mkvkit probe    FILE...                       what is in it
mkvkit chapters show|check|rollback|apply     the marks, and the document
mkvkit chapters classify|match|windows        somebody else's names: which job,
mkvkit chapters selfcheck|plan                whether they fit, and what changes
mkvkit tags     show                          the tags, and what they overrule
mkvkit propedit FILE --track UID ...          change a header in place
mkvkit remux    FILE --staging DIR            rebuild it without some tracks
mkvkit verify   ORIGINAL BUILT                prove the difference is the one you asked for
mkvkit swap     KEEPER REPLACEMENT            park the old file, put the new one in its path
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
