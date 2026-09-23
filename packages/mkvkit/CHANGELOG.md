# Changelog -- mkvkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

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
  `mkvkit.chapters.names` are still skeletons. The arithmetic half of that
  question is in `chapters.grid`; the half that needs the content is not here.
- `mkvkit.plan` -- the policy-driven planner over a whole collection and its
  churn report -- is still a skeleton. `mkvkit.remux` plans one file.
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
