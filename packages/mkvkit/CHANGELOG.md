# Changelog -- mkvkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

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
