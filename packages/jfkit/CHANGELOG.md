# Changelog -- jfkit

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## Unreleased

### Added

- **The device gate sees what used to hide from it, and says which signal
  held it.** A decoder whose command line cannot be read (the server's, run
  as another account) now holds the gate instead of being ignored; the
  device's own counters are sampled for a second without privilege; and the
  server's sessions, running tasks and recently changed items are read.
  `jfkit jobs gate` names every signal that made it RED and gained
  `--sample`, `--recent-window`, `--lock`, `--wait` and `--json`.
  `jobs.observe()` and `jobs.LaneGate` give the same to Python callers, the
  second as a ready `before_each` for `lanes.map_by_device`.
- **`jfkit.validation`: a change at a library's top level is said to cost a
  whole-library validation, before it is made.** For the verbs that remove,
  park, move or rename.
- **`jfkit.healthlink`: the server's half of `mkvkit health`** -- its gate,
  and each suspect file's item id and title.
- **`dedupe`.** Copies of one film or episode resolved into one keeper and
  parked copies, as one audited plan. Films are grouped by provider
  identifier, episodes by series, season and episode plus a shared
  identifier -- never by a name -- and the segments of one episode are kept
  apart. Every copy is probed one reader per disk; the keeper is the copy
  that may replace every other under `[policy]` and `[policy.dedupe]` (audio
  languages at equal or more channels, lossless and commentary tracks, kept
  and forced subtitle languages, the running time), ranked by lossless,
  channels, source, resolution class and bitrate; a conflict keeps both.
  The keeper's payload is read with `mkvkit integrity` before anything is
  planned, and a keeper that fails blocks its group. The plan carries every
  user's watched state onto the keeper, parks the other copies with their
  sidecars, parks release folders left with no video and notifies narrowly.
  Dry run by default; `--apply` with an audit; resumable.
- **A `resolved-duplicate` category for `delete`.** It parks a copy whose
  watched state is non-blank, once every user's state is on the kept item,
  and checks that again at the moment of the park.
- **`userdata.merge` and `userdata.carries`.** One user's state across
  several copies of one film as one row, and whether one row holds another.

- **`leftovers sweep` and `leftovers missing`.** A walk of every media
  library (never through a link or a junction, one device after the
  other, each behind the device gate) sorts every file into junk,
  protected, video, sample and unsure by rules that are data, and proposes
  release junk, dead release folders, samples and -- with `--corrupt` --
  unplayable files. Proposals pass the `delete` preconditions and become a
  `mkvkit.steps` plan that parks, checks every candidate again as it runs,
  and resumes from its audit. `missing` reports rows with no file, files the
  walk did not find, gaps in a season's numbering, and release folders whose
  release is not catalogued.
- **Four leftover categories for `delete`.** `release-junk`,
  `dead-release-folder`, `corrupt-unplayable` and `sample` take no item
  identifier: a candidate passes when nothing catalogued lives at or below
  it, it holds no link or junction, and what the category needs is true.
  `corrupt-unplayable` needs failed integrity evidence and no other
  catalogued copy, and its evidence goes into the audit. `delete --rules
  FILE` widens the junk rules and `--integrity full|quick` says how an
  unplayable candidate is measured.
- **`find`, `children` and `playstate`.** Items by name, path, provider id
  or type; an item's children, descendants or extras; and every user's
  watched state for items, read as each user. All user-scoped, printed as
  tab-separated rows or JSON, `playstate` batched and sent `--jobs` requests
  at a time.
- **`userdata snapshot|replay|verify`.** Every user's watched state, carried
  across a rename or a renumber through an old-to-new identifier mapping.
  The replay also clears state the server handed a new episode by its
  season/episode slot, which the snapshot does not account for, and reports
  the last-played dates it cannot clear. It is a `mkvkit.steps` plan: dry
  run, audit, resume.

- **`rename`.** Rename videos or folders as one audited plan: one pair, or a
  tab-separated mapping of many. Every target is predicted with the naming
  port and checked against what was intended, for path length with the
  preview tiles, and for collisions on disk, among the targets and in one
  season and episode slot; any mismatch refuses the plan. Sidecars move with
  their video and a stale `.nfo` is parked outside the library. Watched
  state is snapshot for the whole series first. The renames are
  `mkvkit.steps` actions, and chains and cycles go through temporary names;
  the run is audited and resumable. Only the deepest changed folders are
  notified: a library root, and an unknown folder below one, are refused.
  The run then waits, bounded, for the new items, replays the watched state
  (clearing rows inherited by slot) and verifies the end state. It is a dry
  run unless `--apply --work DIR`.

- **`delete` proves the kept copy plays.** Before a twin, a superseded copy
  or a rebuild's donor is parked, the payload of the copy that is kept is
  read with `mkvkit integrity` -- sampled, listed and, by default, decoded
  (`--keeper-check full|quick`). A kept copy that fails, or that cannot be
  checked, refuses the candidate: every earlier check compared headers,
  which a file that was never filled keeps intact. Each kept file is read
  once per run and its report goes into the audit log.
- **`swap` proves every replacement plays, before the first outage.** Each
  replacement is read once, before any chunk stops the service, and a pair
  whose replacement fails or cannot be checked is refused
  (`--replacement-check full|quick`).

### Fixed

- **One parking layout, drive letter kept.** `delete` and `swap` dropped
  the drive of a parked path while `leftovers` kept it, so the same library
  path on two disks landed on one place and the second park was refused.
  Every verb that parks now keeps it (`C:/Media/Movies/x` goes to
  `<parked>/C/Media/Movies/x`), through `mkvkit.transfer.parked_relative`.
- **`dedupe` and `leftovers` wait on the whole gate.** Both asked the old
  gate only about named readers and running tasks, so a disk somebody was
  playing from, a reader whose command line is hidden, or a lock file did
  not hold them. They now use the gate `mkvkit health` waits on
  (`healthlink.lane_gate`): `dedupe` looks before every copy it reads and
  gives a copy up after `--gate-wait`; `leftovers` looks once per device and
  leaves a RED device out.
- **`survey duplicates` does not call segments duplicates.** Files named as
  parts of one episode (`S01E01a`, `S01E01b`, `part1`) share its identifiers
  and were grouped as copies. The segment is part of the group key now, and
  identifiers shared by distinct segments are counted separately.
- **`delete` parks a video's sidecars with it.** The description file,
  pictures and preview tiles that belong to a parked video by the server's
  rules follow it into the parking directory, instead of staying behind for
  a second, hand-made pass; subtitles and external audio follow only when no
  copy is kept. A folder left with no video is said so.
- **`delete` can release a folder that is no item's path.** A
  `media-free-folder` candidate with no identifier is checked against the
  whole catalogue -- nothing catalogued at or below it -- instead of being
  refused because its own path is not an item's path, which a release
  subfolder's never is.
- **`delete` does not call a folder media-free when it holds a link.** A
  junction or symbolic link below a released folder was walked into -- onto
  whatever disk it pointed at -- or ignored. The check now walks without
  following links, and a link, a junction or a folder it cannot list makes
  the folder not media-free.

- **`jfkit delete --apply` no longer asks the server to delete the item.**
  After parking a file it called `DELETE /Items/{id}`, which on this server
  removes the item's *containing folder* from disk -- sidecars, artwork,
  extras and any other film in the same folder -- and nothing could turn it
  off. The row now goes by telling the server the parked path was deleted
  (`POST /Library/Media/Updated`, `UpdateType: Deleted`); its scan drops the
  row whose file is gone and deletes nothing. A notification never names a
  library root, and the report says when the row had not gone yet. The test
  stand-in now models the item delete as the folder delete it is, so a test
  that reached it would lose files.
- **The play-state check consults everybody by default.** With no `--user`,
  `jfkit delete` checked nobody and passed with "0 user(s) checked", and
  `jfkit swap` snapshotted nobody. Both now list every user the server has
  (`jfkit.dto.every_user`, `GET /Users`) and check each; a user list that
  cannot be read, or is empty, refuses the apply.
- **A twin or kept item that is the candidate itself is refused.** A file is
  byte-identical to itself, so a manifest naming the candidate as its own
  twin -- or its own item as the kept one, or a kept item whose file is the
  candidate's -- passed every check.
- **`jfkit swap` waits for the record to show the new file.** Verification
  took the first read after the refresh, usually the record from before the
  swap, and reported success against it. It now polls until the stream count
  the plan gives (an optional fourth plan column) or, failing that, a changed
  stream table or chapter list; a record that never gets there is a problem
  and a non-zero exit.
- **`jfkit swap` checks the plan's path against the item's catalogued path**
  before stopping the service. A plan line pairing one item's identifier with
  another file used to swap that file and report success.
- **The `jfkit swap` dry run reads every precondition.** It skipped them all;
  it now reports a missing item, a path mismatch, a missing file and an
  unreadable user list, and exits non-zero, without writing anything.
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
- **An applied database pass copies and counts after the service has
  stopped, not before.** The rollback copy and the before-counts were taken
  while the server was still up, so both described a state from a moment
  before the one that was rewritten. The order is now stop, copy, count,
  apply, count, restart.
- **A generator of operations is applied, not only described.** The
  describe step consumed it, the apply step found nothing, and the pass
  reported ok. The operations are listed once.
- **A dry-run database pass without `--snapshot-dir` reads a temporary copy**
  and removes it afterwards, instead of describing from the live file through
  a read-only connection while the runbook said otherwise.
- **`maintenance run` works with no server configured.** The busy check is
  skipped when the configuration has no way to get a token, as intended,
  instead of failing on the missing server. `jobs gate` uses the same test.
- **Library options with populated per-type options are no longer
  flattened.** Fetcher lists and image options were read as the whitespace
  between their elements and sent back that way. The two record lists are now
  parsed against their known shape and rendered back to prove the
  round-trip; anything outside that shape is refused rather than written.
- **Applied writes to server records keep a rollback artefact, always.**
  `item set --apply` requires `--backup PATH`, `libopts set --apply` requires
  `--backup-dir DIR`, and `segments scope --apply` requires a new
  `--backup-dir DIR`; each refuses without it, with the flag to pass, and
  sends nothing. `segments.scope_plugin()` refuses an applied call without
  `backup_dir` in the library too, and writes the configuration as it was
  before posting.
- **`segments scope` keeps the exclusions that were already there.** It
  replaced both lists with what the coverage found, dropping every exclusion
  somebody had set by hand. The new lists are the old ones plus the covered
  items, without duplicates; a list of another shape is refused.
- **The dry run logs each write's body**, as the documentation said, with
  credential-looking values (`token`, `password`, `api_key`, ...) replaced
  by `<redacted>`. It logged the body's size.
- **Service control takes the service's name.** `--service windows` and
  `--service systemd` now need `--service-name NAME` (and `--service-program`
  selects NSSM); without it the command refuses before anything is stopped.
  The name was hardcoded, so the Windows controller always failed. NSSM is
  asked with `status`, which it has, instead of `query`, which it does not.
- **Detached jobs honour `Job.environment` and `Job.log`** on the
  transient-unit form (`--setenv=`, output appended to the log), and the
  scheduled-task form, which cannot carry either, refuses a job that sets
  one instead of silently dropping it.
- **A scheduled task is not overwritten, and it actually runs.** The task
  was created with `/F`, replacing any task of the same name, and with a
  one-off midnight trigger that is normally already past and no explicit
  run, so it never started. `launch_detached()` now refuses when a task of
  that name exists (unless `replace=True`), creates without `/F`, and then
  starts it with `/Run`. `launch_detached()` returns the list of commands;
  `detached_commands()` is new.
- **`delete` refuses a media-free folder that still holds a track.** A
  `media-free-folder` candidate holding an external audio track or a
  subtitle is refused: no video in it does not mean nothing irreplaceable
  in it.
- **`notify` refuses every library root the server has.** It knew only the
  two folders in `[paths]` and any `--root`, so a library on another volume,
  or one added later, could be notified whole -- a full scan. It now reads
  every library's folders from the server first, as `delete` already did,
  and sends nothing when they cannot be read. `refresh.library_roots()` is
  new.
- **`refresh --expect FIELD` says when FIELD did not change.** It was only
  an allowance: an expected field that stayed as it was went unreported, and
  with no settle condition the first read -- usually the record from before
  the refresh -- was the answer. A field named with `--expect` is now waited
  for until it moves or the record's refresh marker (`DateLastRefreshed`,
  `Etag`) does; one that did not move is reported with the value it kept and
  the command exits 1. `safe_refresh(require_changes=...)` is the library
  side.
- **`naming` says what its exit status means, and what will not fit.** The
  help states it: 1 when any name would be read as a range (a double
  episode named that way on purpose included), 0 otherwise. Each name is
  measured with the longest file the server writes beside it and marked
  `LONG` above the classic Windows limit of 259 characters.
- **Long lists no longer have to fit on one command line.** `naming`,
  `notify` and `refresh` take `-` (one entry per line on standard input) and
  `@FILE`; `naming --full-paths` prints each path as given, so batched
  output can be matched to its input.
- **`delete` notes what removing a top-level folder costs.** A folder
  candidate directly under a library folder is reported, in the output and
  the audit, as one whose removal makes the server refresh that whole
  library -- not refused, but worth scheduling for a quiet disk.
- **`item show --save` keeps stdout for the record.** The "written to" line
  went to stdout ahead of the JSON, so a pipe into a JSON reader failed on
  its first line. It goes to stderr now.
- **`item set` reports the fields it changes, not the record that carries
  them.** Each phase prints and logs `Field: was -> now`; the dry run logs a
  body over a kilobyte by its size and prints it whole with `-v`, where it
  used to put the whole record on one line at the default level.
- **A refused request closes its response.** Every error status left the
  server's response open until the garbage collector found it, one per
  refusal and one per retry. It is read and closed where it is caught.

### Packaging

- **Where it comes from is written down.** The package metadata names the
  repository, its source and its issue tracker (`[project.urls]`), the source
  distribution now carries this changelog, and the build backend is required
  at the version that understands the `license-files` field the metadata
  uses. The package is not on PyPI; the README says how to install it from
  the repository, together with `mkvkit` in the same command so that pip
  never looks for it on PyPI.

## 0.3.0 -- 2026-09-23

The server side, finished. Every module that was a skeleton at 0.1.0 is now
implemented, tested and documented, and the four that write default to a dry
run.

There is no 0.2.0: that number was reserved for the file-side package, which
reached it first, and the two are versioned independently.

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
