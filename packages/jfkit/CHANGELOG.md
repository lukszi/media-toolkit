# Changelog -- jfkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## Unreleased

### Fixed

- **An applied database pass is refused when it has nowhere to put the copy
  it takes first.** `maintenance.run_operations()` documented the copy as a
  standing step of every applied run -- stop, copy, apply, restart, count --
  but took it only when the caller named a directory, so
  `maintenance run --apply --reindex` rewrote a file nothing else had a copy
  of and produced no rollback artefact at all. The library refuses it now and
  the command line refuses it earlier, with the flag to pass. Found by reading
  the writing paths against the safety claim in the README rather than by
  anything failing.
- **The structural test over the command line now recurses.** It walked the
  top level only, so the six verbs that live under a group -- `item set`,
  `libopts set`, `maintenance run`, `maintenance previews`, `segments scope`,
  `segments cancel` -- were never checked for having two states at all. It
  names them now, so removing an `--apply` from any of them fails.

## 0.3.0 -- 2026-09-23

The server side, finished. Every module that was a skeleton at 0.1.0 is now
implemented, tested and documented, and the four that write default to a dry
run.

There is no 0.2.0: that number was reserved for the file-side package, which
released it first, and the two are versioned independently.

### Added

- **`jfkit.dto`** -- one record round-trip in place of a folder of near-copies.
  Fetch the whole record from the user-scoped route, remove the preview block
  or the request fails with a server error, send the **whole** object back
  because a field left out of the body is emptied rather than left alone, and
  write the phases in the one order that survives: numbers, a non-replacing
  refresh, names, dates last, the metadata lock last of all and never on a
  folder, series or season. `COPIES` and `DISCARDS` are data with a test
  asserting them against a stand-in server. `compare()` diffs two records
  over names, overview, provider identifiers, chapters (with the deltas in
  milliseconds), media streams and play state, and normalises generated
  chapter names away, because a file whose marks are unnamed differs from
  itself in every one of those strings.
- **`jfkit.refresh`** -- one safe refresh in place of several divergent copies.
  Non-replacing by default, polled until a caller-supplied condition holds
  rather than read once and believed, and diffed against the changes that were
  expected. Two guards ship with it: a cleared name does not come back from a
  provider where a sidecar sits beside the media, and applying a searched-for
  identity empties the air date and production year, which are snapshotted and
  written back. `notify_changed()` is the narrow path nudge, and it refuses a
  library root -- that is not a notification, it is a full validation of
  everything below it.
- **`jfkit.libopts`** -- library options with the omitted fields filled in
  from the constructor-defaults table, which is data here with a test. A write
  re-reads the document afterwards and asserts that exactly the intended
  fields moved. `root_path_problems()` is the two-line check for the symptom
  that is hardest to attribute: every library listed with no identifier and no
  options, because the data directory moved and the route matches records by
  comparing paths exactly. `toggled()` sets an option for the duration of a
  pass and puts it back, including when the pass raises.
- **`jfkit.maintenance`** -- the database, with the guards rather than the
  statements as the content. Reads go through a copy made by a read-only
  connection; writes go through a service controller, a snapshot, and a
  row-count comparison afterwards. A fix is a named operation with a
  precondition -- `RepointPaths` refuses unless the expected number of rows
  match and every rewritten path exists -- and there is no route for a
  statement somebody types once. `restore_previews()` puts back generated
  tiles additively: never overwriting, never deleting, on either side.
- **`jfkit.report` and `jfkit.surveys`** -- one survey shape and six surveys:
  metadata completeness, audio languages per track, chapter state, a container
  census, duplicates by provider identifier, and filename-parse prediction.
  Each renders to TSV, CSV, JSON, Markdown and HTML from one call,
  deterministically, with its scope and its caveats **inside the document**.
- **`jfkit.swap`** -- the in-place swap, around the file-side one. Wait until
  nobody is watching, stop the service per chunk, park the original, copy the
  rebuild into its exact path, read it back, restart, refresh, and compare the
  record. Chunks are sized in bytes and can be held to a time budget, because
  a chunk of short episodes and one of long films can be the same file count and
  two very different outages. Play state is snapshotted for every user and replayed.
- **`jfkit.safedelete`** -- nothing is deletable until a category is released
  by name; every precondition is checked against the world rather than against
  the manifest; nothing is deleted, things are moved; the catalogue row goes
  only after the file has arrived; and every step is logged, including the ones
  that did nothing. The evidence half -- size-then-digest identity, stale-path
  remapping, and the folder walk that separates sidecars from other media --
  is independently useful.
- **`jfkit.segments`** -- scope a segment analysis to media that is not
  already covered, and do not start it while the disk is busy. No plugin or
  task identifier is written down: both are looked up by name, and a name that
  matches two things is an error rather than a coin toss.
- **`jfkit.jobs` and `jfkit.devices`** -- detached jobs from one template, and
  one heavy reader per device. A path is resolved to its mount point before
  anything is compared, and a command line is matched to a device by shape
  rather than by substring; the first version of both got this wrong and put
  two readers on one disk.
- **Ten new sub-commands** on the `jfkit` entry point, with the two properties
  the file-side one has: reading is free, writing is asked for twice, and the
  exit code carries the answer.
- **Documentation**: `docs/jfkit.md`, `docs/methods/swap-procedure.md`,
  `docs/methods/safe-deletion.md`, `docs/runbooks/db-maintenance.md`,
  `docs/patterns/spindle-gate.md`, `docs/patterns/detached-jobs.md`, and two
  fully invented worked recipes under `examples/recipes/`.

### Fixed

- `jfkit.surveys` no longer reports a version of its own. The package has one
  version and it is `jfkit`'s.

### Known limits

- Validated against one server release. Every entry in the gotcha reference
  carries the version it was confirmed on, and the filename predictor names
  the release it was read from; neither is a claim about any other release.
- **Nothing has been installed as a distribution.** No wheel has been built,
  nothing has been uploaded, and no tag exists. The version is a claim about
  this tree.
- The unit-manager service controller is still a deliberate stub that refuses
  rather than guessing. Use the manual controller, which asks first and works
  everywhere.
- There is no manifest builder for deletion, no resume for a swap, and no
  supervision for a detached job. Each method document ends with its own list
  of what is not implemented.

## 0.1.0 -- 2026-09-23

First release. What is here is finished and tested; what is not here is
skeletons, and they say so in their own docstrings.

### Added

- **`jfkit.naming`** -- predicts how a media server will read a filename,
  before anything is renamed. A port of the episode-path expressions of one
  release, pinned by a table of sixty-five invented names, with the version it
  was checked against recorded in the module. The two shapes the ported
  expressions cannot claim are reported as such rather than as "no match".
- **`jfkit.client`** -- a standard-library HTTP client. The token belongs to
  the client object, is resolved from the configured indirection when it is
  first needed, and is registered with the redaction filter in the same step,
  so it cannot reach a log. Item queries are **user-scoped by default**,
  because the unscoped collection route omits items rather than failing.
  Writes are opt-in: a client is constructed in dry-run mode, where a mutating
  request is logged in full and not sent. Timeouts and a bounded retry, and
  only idempotent requests are retried.
- **`jfkit.client.wait_idle`** -- refuse to start while somebody is
  mid-episode. Every destructive pipeline should call it.
- **`jfkit naming`** on the command line, with an exit code that means
  something: non-zero when any name would be read as an episode range.
- **`docs/gotchas/jellyfin-12.md`** -- the server reference, grouped and
  version-stamped, with the three entries that look like upstream bugs marked
  as such.

### Known limits

- Validated against one server release. Every entry in the gotcha reference
  carries the version it was confirmed on, and the filename predictor names
  the release it was read from; neither is a claim about any other release.
- `swap`, `refresh`, `dto`, `libopts`, `maintenance`, `surveys`, `segments`,
  `safedelete` and `jobs` are skeletons. They land at 0.3.0.
- The client covers the routes this release needs and no more: items,
  sessions, and whatever a caller asks for by path.
