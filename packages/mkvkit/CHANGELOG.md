# Changelog -- mkvkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## Unreleased

Every finding of the pre-release review that concerns this package's own
code. Most are about a write that could leave a file worse than it found it
and not say so.

### Added

- **`mkvkit integrity` reads the payload, not the headers.** A file whose
  body was reserved and never written keeps a perfect header, and every
  header-level reader -- `probe` included -- called it healthy. The check
  samples evenly spaced blocks for zero fill, lists every packet of every
  audio and video track against the container's duration, and decodes
  every track, reporting each track's decoded duration and what the
  decoder complained about. A file that cannot be read, or a missing
  program, is reported as no evidence, never as a pass. The library side is
  `mkvkit.integrity.check()`, `sample_zero_fill()` and `Thresholds`.
- **`probe` looks at the payload a little, and at every track's duration.**
  It samples sixteen blocks and reports zero-filled ones (a problem, exit 1,
  above the integrity threshold), and where a track's stated duration
  differs from the container's by more than a few seconds it prints each
  track's duration and a warning. `Stream.tagged_duration_s` and
  `probe.duration_disagreements()` are new.
- **`mkvkit copy SOURCE DESTINATION [--move]` copies and proves it.** It
  refuses an existing destination, copies to a partial file (optionally in a
  `--stage` folder on the destination's volume) while hashing the source,
  hashes the copy again from the destination side, renames it into place
  without replacing anything, and removes the source of a move only after
  that. Every report says that a read straight after the write is usually
  served from the operating system's cache. `mkvkit.transfer.verified_copy()`
  is the library side.
- **`swap` proves the replacement plays before the original is parked.**
  The payload check runs in the dry run too; a replacement that fails it,
  or cannot be checked, refuses the swap. `--replacement-check full|quick`
  chooses whether it decodes; nothing skips it.

### Changed

- **`remux --apply` verifies what it built.** Both files are hashed stream by
  stream and compared against the rebuild's own plan before the command
  exits; a rebuild that does not verify exits 1 and is left in staging. The
  printed `mkvkit verify` line now carries `--default-moved` and `--chapters`
  where the plan needs them.
- **Moving the default audio track clears it everywhere else.** The muxer is
  told `:0` for every other kept audio track, so a rebuilt file never carries
  two defaults; `verify --default-moved` now requires exactly one.
  `verify --chapters DOCUMENT` holds a rebuild's marks to the document that was
  written in.
- **Applied header edits write their rollback to disk first.** `propedit`,
  `chapters apply` and `chapters plan` take `--rollback-dir` (default
  `<[paths].work>/rollback`) and write a TSV of previous values plus the chapter
  and tag documents the file had -- exactly as the extractor printed them --
  before the editor runs. An edit whose rollback cannot be written does not
  happen. `Rollback.restore_command()` gives the editor arguments that undo it.
- **Tags round-trip whole.** Nested simple tags, binary values and their
  format, `TagLanguageIETF` and the default-language flag are read and written
  back; the triple comparison names nested tags by path and sees an emptied
  binary value. A tag document with any other element is refused before
  anything is written.
- **Chapter structure the model cannot carry is refused, not dropped.** Marks
  nested under a mark, and a mark named in more than one language, are
  recorded on read; `build()` and `rollback()` raise, and `chapters apply`,
  `chapters rollback` and `remux --chapters` refuse.
- **`--out` is never overwritten silently.** `chapters rollback` and
  `chapters match` refuse an existing `--out` file unless `--force` is given.
- **`remux` refuses, rather than deletes, a file already in staging** under
  the output name, its `.part` name or its chapter-document name, and removes
  its own part file and chapter document whatever happens.
- **A credential in `[server].url` is refused** (`user:pass@host`), without
  repeating it in the error.
- **`[langid].model_dir`** sets the speech model's cache directory; it was
  documented as configuration and was not.
- The `align` extra is gone: it installed numpy and scipy, which nothing in
  this package uses.

### Fixed

- `swap`: a check that raised after the copy (a failing probe, Ctrl-C) left
  the unverified file live. The original is now put back before the error
  travels on.
- `remux` and `propedit`: a warning exit with no message text raised
  `IndexError` instead of recording the warning.
- The chapter-database adapter's disabled message named a command-line flag
  that does not exist; it now names the constructor argument.
- `server.user_id` refused the bare 32-digit form the server's own API
  returns. Both forms are accepted now and stored as the dashed lower-case
  one; anything else is still refused.

### Packaging

- **Where it comes from is written down.** The package metadata names the
  repository, its source and its issue tracker (`[project.urls]`), the source
  distribution now carries this changelog, and the build backend is required
  at the version that understands the `license-files` field the metadata
  uses. The package is not on PyPI; the README says how to install it from
  the repository.

## 0.3.0 -- 2026-09-23

Chapter names: where a published list comes from, whether its names describe
the marks they sit on, and how to write names of your own without writing
something confidently wrong. The last skeletons in this package are gone.

### Added

- **`mkvkit.chapters.sources`** -- a protocol for anything that can offer
  chapter lists, one worked adapter for the shape those archives have, and the
  classification that decides what may be done with a candidate: names onto
  marks you already have, times *and* names onto a file with none, or times
  only. The adapter ships **disabled**, caches on disk, rate-limits itself and
  says what it is; the fetching is injected, so nothing in the test suite
  opens a socket. A candidate under half real names is a list of times: a
  count that only tested for a non-empty string once wrote lists of pure
  labels into files.
- **`mkvkit.chapters.verify`** -- the half of the question that needs the
  content. Twenty seconds of original-language audio from each mark and one
  frame shortly after it, **both out of a single seek**, behind pluggable
  evidence providers; the content-word hit rate of the name list scored at
  offsets −4 to +4, where a clearly better score away from zero is a list
  typed against different marks; corroboration from any second published list,
  which also catches a list that belongs to another film entirely; and a
  three-way verdict recomputed centrally from the per-mark calls, of which
  only `ALIGNED` may be written.
- **`mkvkit.chapters.names`** -- the matcher, and the rules. A list that fits
  better one mark along is **refused, never slid into place**. The rules come
  in two strengths: a timecode or a bare label is not a name at all, while
  length, punctuation and language are advisory for a name off a disc and
  blocking for one a program just wrote. A chapter with no dialogue gets the
  structural name where one belongs and the empty string everywhere else. A
  provenance tag records where the names came from, merged into the file's own
  tags rather than replacing them.
- **`mkvkit.chapters.windows`** and **`mkvkit.chapters.transcripts`** -- one
  window of text per mark, sized by the chapter's duration and sampled evenly
  across its whole span rather than head-and-tail, because a flat cap elides
  the middle of a long chapter and the middle is what the chapter is about. A
  cue crossing a mark is split at it, a transcriber's long segments are re-cut
  from word timings, and a transcript that is a stub or stops early is refused
  with the reason rather than described.
- **`mkvkit.chapters.selfcheck`** -- the grading pass, with coverage,
  duplicates, order words, language and length as mechanical checks and four
  grades. **Any `wrong` grade holds the whole film back**, and the bar is
  applied in one place whoever produced the grades.
- **`mkvkit.plan`** -- a per-file plan: chapters, tags, track edits and title
  in one object, printable as the dry run, carried out in a single invocation,
  and comparable with the previous run's plan through `explain_churn()`.
- **`mkvkit chapters classify | match | windows | selfcheck | plan`** on the
  command line, dry run by default. The judging half runs from a recorded
  evidence file, so it needs no decoder, no model and no media.
- **`docs/methods/chapter-names.md`**, leading with the misalignment finding as
  a piece that stands on its own, plus five new entries in the container
  reference.

### Changed

- `is_generic_name()` knows the word for a chapter in three more languages. A
  word missing from that list is a label counted as a name.

### Known limits

- **There is no namer, and there will not be one here.** The writing step in
  the work this came from was a person reading the windows. What ships is the
  window builder, the rules, the self-check and the verifier; `mkvkit
  chapters` has no `name` verb.
- The per-mark call reads the transcript, not the picture. The frames are
  collected for a person, and a reader's calls can be passed into the verdict
  instead.
- Nothing here finds subtitle files. The window builder takes cues, and
  matching sidecars to films by walking directories is more dangerous than it
  looks -- see the method document.
- Nothing has been installed as a distribution or built as a wheel. The
  version is a claim about this tree.

## 0.2.0 -- 2026-09-23

The file side: reading a file, changing one thing in it, and proving that only
that thing changed. This release is useful with no media server anywhere.

### Added

- **`mkvkit.probe`** -- one typed reading of a file from both programs that
  can describe it. Canonicalises the two spellings of a language code, reports
  a tag that overrules a track header (from either program), answers whether
  there is a seek index, and guards against a file whose extension promises
  Matroska and whose container is something else.
- **`mkvkit.ebml`** -- rewritten, typed and tested: the top-level element scan
  behind that last answer, following the seek index where there is one and
  walking the segment only when there is not.
- **`mkvkit.chapters.xml`** -- read, write, self-check and roll back a chapter
  document. Eleven blocking checks and two notes, each of them earned. An
  unnamed mark is written with no display block, never with the label a player
  would show anyway.
- **`mkvkit.chapters.grid`** -- does a published chapter list describe your
  cut? Compared at three scales with the median offset removed; within two
  seconds is a match. A rate-converted candidate may have its names copied
  onto your own marks and may never have its marks written into a file that
  has none.
- **`mkvkit.tags`** -- the element that overrules the track header. Read,
  merge, write, and a provenance tag that is idempotent. Comparison is by
  `(track identifiers, name, value)` triples, which is the only comparison
  that survives the editor's own normalisation.
- **`mkvkit.propedit`** -- one in-place header edit, with the earlier one-off
  implementations merged into it. Selects a track by its identifier, refuses a
  language change a tag would overrule, compares before and after keyed on the
  identifier, and captures a rollback whether or not it writes.
- **`mkvkit.verify`** -- evidence on both sides against a *declared*
  difference: tracks dropped, tracks appended, or a header edit. One hash per
  stream as the primary evidence, a per-frame comparison to tell a re-encode
  from a container rewrite, notes with reasons for the differences that cannot
  matter, and a header-only path that keeps the hashes already collected.
- **`mkvkit.remux`** -- decide what a rebuild keeps from policy, with a
  sentence per track, then build it into a staging directory. Never drops the
  original language, an unlisted language, an untagged track or a commentary
  track; refuses a plan that would leave nothing recognisable; pairs a chapter
  document with the option that suppresses the file's own marks.
- **`mkvkit.swap`** -- park the original, put the rebuild in its exact path,
  read it back in place, and restore on any failure.
- **`mkvkit probe | chapters | tags | propedit | verify | remux | swap`** on
  the command line. Everything that writes defaults to a dry run; every exit
  code means something.
- **`docs/mkvkit.md`** and **`docs/methods/verification-discipline.md`**, and
  five new entries in the container reference.
- A fixture carrying a language tag that contradicts its own track header --
  the one file the encoder cannot produce on its own.

### Known limits

- `mkvkit.chapters.verify` (were these names typed against these marks?) and
  `mkvkit.chapters.names` are still skeletons at this version. The arithmetic
  half of that question is in `chapters.grid`; the half that needs the content
  is not here. Both land at 0.3.0.
- `mkvkit.plan` is still a skeleton at this version. `mkvkit.remux` plans one
  file.
- Nothing has been installed as a distribution or built as a wheel. The
  version is a claim about this tree.

## 0.1.0 -- 2026-09-23

First release. What is here is finished and tested; what is not here is
skeletons, and they say so in their own docstrings.

### Added

- **`mkvkit.langid`** -- spoken-language identification, in three layers that
  can be run independently.
  - `worker`: activity-gated window sampling, one decoder invocation per
    window, a fallback to a single sequential decode when the container has no
    usable index, and an append-only result log keyed by path, stream and
    model so an interrupted run resumes.
  - `ladder`: aggregation as a geometric mean of per-window posteriors, an
    asymmetric settle bar (confirming a tag is cheap, overturning one is not),
    and three context rules -- tag-confirm, prior-backed, and a trimmed
    aggregate that needs corroboration from two independent stages.
  - `priors`: release-name and sibling priors, each of which can only ever
    lower a bar, and each blocked outright for the material where context is
    systematically misleading.
  - `review` and `report`: outcomes, a re-scan queue where every entry says
    what the *next* pass should do differently, and a report that leads with
    what did not settle.
  - `sources`: a filesystem walk as the default job source, and a catalogue
    adapter that takes any object able to answer an item query, so this
    package neither imports a client nor depends on a server.
- **`mkvkit.langcodes`** -- one canonicalisation of the three language-code
  spellings that meet in a media file, so a track tagged one way and a
  detection spelled another are not a disagreement.
- **`mkvkit langid jobs | scan | report`** on the command line. `report` runs
  with nothing installed at all.
- **`docs/gotchas/matroska-ffmpeg.md`** -- the container and probe reference,
  entry by entry, each with how it was confirmed.

### Known limits

- The thresholds in `SettleBar` were fitted against one collection's
  composition. They are defaults and configuration, not universals; the method
  for re-fitting them ships with them.
- The detector protocol has one implementation (a Whisper-family model, behind
  the `langid` extra). The ladder's fourth stage wants a second *family* and
  cannot be exercised until there is one.
- `propedit`, `verify`, `tags`, `chapters`, `plan` and `probe` are skeletons.
  They land at 0.2.0.
