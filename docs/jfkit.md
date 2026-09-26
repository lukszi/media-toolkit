# `jfkit`: the server side

Tools for asking a media server what it thinks it has, changing one field of
it without emptying the rest, and replacing a file underneath it without
losing what the catalogue knows.

Install, from a checkout of the repository (`jfkit` is not on PyPI, and
`pip install jfkit` from PyPI is not available):

```
git clone https://github.com/lukszi/media-toolkit
cd media-toolkit
pip install -e packages/mkvkit -e packages/jfkit
```

or without a checkout, both in one command:

```
pip install "mkvkit @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/mkvkit" \
            "jfkit @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/jfkit"
```

`jfkit` needs `mkvkit`: the two packages share one configuration model, one
program-discovery path and one logging setup. Name `mkvkit` in the same
`pip install` command, as above, so that pip takes it from the repository and
never goes looking for it on PyPI.

Validated against Jellyfin 12.1. Not affiliated with the Jellyfin project.

Every example below uses invented names.

---

## First, two things that are not optional

**Queries are user-scoped.** The unscoped collection route does not fail on
this server version -- it *answers*, with fewer items than exist, and gives no
indication that anything is missing. An audit built on it under-reports and
says nothing about it. `jfkit.client.Client.items()` is user-scoped by
default, and asking for the other route is an explicit act that logs a
warning.

**Writing is asked for twice.** Every command that changes anything takes
`--dry-run`, which is the default, and `--apply`. In the dry run every
mutating request is logged -- method, address and body, with any
credential-looking value in the body replaced by `<redacted>` -- and not sent,
so a whole pipeline can be run against a real server and produce a complete
account of what it *would* do. A body over a kilobyte -- a whole item record,
say -- is summarised by its size and printed whole with `-v`; `item set`
reports the fields it changes, before and after, on its own. There is no third
state and no environment variable that flips it.

**An applied write to a server record keeps a rollback.** Three verbs refuse
`--apply` until they are told where to put the state they are about to
replace, and write it there before anything is sent:

| verb | flag | what is written |
|---|---|---|
| `jfkit item set` | `--backup PATH` | the whole record, as JSON |
| `jfkit libopts set` | `--backup-dir DIR` | a stamped copy of the options document, and its values as JSON |
| `jfkit segments scope` | `--backup-dir DIR` | the plugin's configuration, as JSON |

`jfkit maintenance run --apply` refuses without `--snapshot-dir DIR` for the
same reason, and copies the database there after the service has stopped.

---

## The shape of a job

```
jfkit naming      PATH...                  how a filename will be read, before renaming
jfkit survey      NAME                     read-only inventories of the library
jfkit item        show|set|diff ID         one record: read it, change it, compare it
jfkit refresh     ID...                    refresh without replacing, and diff the result
jfkit notify      PATH...                  tell the server one path changed
jfkit libopts     show|roots|set           a library's options, defaults filled in
jfkit maintenance snapshot|check|run|previews   the database, and the preview tiles
jfkit swap        PLAN                     put rebuilt files in their items' places
jfkit delete      MANIFEST                 park what a named category released
jfkit segments    tasks|scope|cancel       scope a segment pass to what is not covered
jfkit jobs        gate|lanes               which device backs a path, and is it busy
jfkit find        --name|--path|--provider find items, as the configured user sees them
jfkit children    ID                       children, descendants or extras of an item
jfkit playstate   ID...                    every user's watched state for items
jfkit userdata    snapshot|replay|verify   carry watched state across a rename
```

`naming`, `survey`, `find`, `children` and `playstate` read and change
nothing, and so does `item` except for `item set`, which writes. `naming`, `survey`, `item show` and
`item diff` are the ones worth running first, and `jfkit survey` is the
cheapest useful thing in the package.

---

## Configuration

One file, found at `--config PATH`, then `$MEDIATOOLKIT_CONFIG`, then
`./mediatoolkit.toml`, then the per-user location.

```toml
[server]
url       = "http://127.0.0.1:8096"
token_env = "JFKIT_TOKEN"      # the token is never a value in this file
user_id   = "00000000-0000-0000-0000-000000000001"

[paths]
parked  = "/srv/parked"
staging = "/srv/staging"
movies  = "/srv/media/movies"
series  = "/srv/media/series"
```

A literal `token = "..."` is refused at load time with an explanation. The
token belongs to the client object, is resolved from the indirection the
moment it is first needed, and is registered with the logging redaction filter
in the same step -- so a value can only become usable by first becoming
unloggable.

---

## `jfkit.naming` -- predict the parse before the rename

A filename decides an item's season and episode numbers, and one shape of
filename decides that it is a *range* of episodes -- which is not writable
through the API at all and is refilled from the path on any refresh that runs
a provider. The only cure is a rename, and the cheapest moment to find out is
before it.

```
jfkit naming /srv/media/series/Northwind --only-problems
```

Exits 1 when any name would be read as a range -- a double episode named
that way on purpose included, so read the `RANGE` lines -- and 0 otherwise; a
name no expression claims does not change that. Each name is also measured
with the longest file the server will write beside it (the preview tiles are
the deepest) and marked `LONG` above 259 characters, the classic Windows
limit. `naming`, `notify` and `refresh` take `-` (one entry per line on
standard input) and `@FILE` for lists too long for a command line, and
`--full-paths` prints each path as given.

It is a whole port, not a selection: all twenty-six episode expressions of
12.1 in upstream order, with their flags; the ten multi-episode expressions;
the path parser around them (first match wins, seasons from 200 to 1927 and
above 2500 rejected, an end number only when it is not smaller than the
episode and is not the start of a resolution); the resolver's guards (only a
video suffix or a `.disc` placeholder is an episode; anything the extras rules
claim is an extra); the thirty-five extras rules; and the season-folder
parser. Each expression cites the upstream file and line it was read from, and
`PARSED_AGAINST` names the release and the commit. A partial port was
confidently wrong in exactly the ways that cost a rename: it called
`E16.Title.mkv` unparsed (the server reads episode 16), missed that
`Name-8000 Title.mp4` is season 80 episode 00 and `Name-8.000 Title.mp4`
season 0 episode 0, and read a file in `Featurettes/` as an episode.

What each line says:

- `RANGE` -- the name produces an episode range, and the exit status is 1.
- `EXTRA` -- a file the extras rules claim: `extra (featurette), not an
  episode`. Only the folder the file sits in counts, and the names are exact
  (`trailers`, `backdrops`, `behind the scenes`, `deleted scenes`,
  `interviews`, `scenes`, `samples`, `shorts`, `featurettes`, `extras`,
  `extra`, `other`, `clips`), as are the endings (`-trailer`, `.sample`,
  `-featurette`, ...).
- `WARN` -- the folder is one or two letters away from an extras folder name
  (`Feaaturettes`), so its files are episodes; this is a warning of this tool,
  not upstream behaviour.
- A note names the season a season folder supplies when the name carries none
  (`season_from_folder`), and which kind of expression claimed the name
  (`Rule`: season-episode, episode, absolute, cross, optimistic, part, pair,
  season-folder, by-date).

`--absolute-order` leaves out the optimistic expression, as the server does
for a series shown in absolute order. The library API is `parse(path)` for the
verdict, and `episode_path`, `extra_type`, `season_folder`,
`near_miss_extra_folder` and `longest_derived_path` for the pieces.

It is pinned three ways: a table of invented names that states the behaviours
a rename depends on, a table of every upstream episode, season-folder and
extras test case translated to the invented cast (each row cites its upstream
test), and a test that every expression is present in order. Two expressions
are written differently from upstream where Python's regular expression engine
would backtrack exponentially on deep paths; the rewrites match the same names
and find the same first match, and the module says where.

## `jfkit.surveys` -- six questions, one shape

```
jfkit survey metadata --out work/surveys
jfkit survey audio-languages --format md
jfkit survey chapters
jfkit survey containers
jfkit survey duplicates
jfkit survey filename-parse
```

Every survey produces the same object -- a name, a generation time, the scope
it covered, typed columns, rows, a summary and a list of caveats -- and
renders to TSV, CSV, JSON, Markdown and HTML from one call, deterministically,
so two runs diff cleanly.

**The caveats are inside the document.** A survey is copied, attached, pasted
into an issue and quoted back six months later, and by then the document is
the only thing left that can say what it counted. That is how a number fitted
to one collection becomes a claimed universal three documents later.

Two columns are worth knowing about. `metadata` reports whether each item's
name is a *title* or something derived from its filename -- an item called
`Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv` has a name, a complete-looking
record, and nothing a person would recognise. And `chapters` reports whether
the marks are *evenly spaced*, which is the signature of a tool rather than a
person: the gaps between real chapter marks vary and the gaps in a generated
grid do not.

## `jfkit.dto` -- the record round-trip

```python
from jfkit.dto import fetch, update_item, compare, save

before = fetch(client, item_id)
save(before, "work/before.json")
update_item(client, item_id, {"IndexNumber": 3, "Name": "The Quiet Harbour"})
print(compare(before, fetch(client, item_id), expected=["Name", "IndexNumber"]))
```

From the command line the same write is
`jfkit item set ID --field IndexNumber=3 --backup work/before.json --apply`;
without `--backup` the `--apply` is refused and nothing is sent.

Four things it does that a two-line version does not:

- **fetches the whole record from the user-scoped route** -- the unscoped
  single-item route answers 400 here, which is at least honest;
- **removes the preview block before sending**, or the request fails with a
  server error that says nothing about which field caused it;
- **sends the whole object**, because a field left out of the body is not left
  alone, it is emptied. This is the failure that turns a two-field edit into
  a lost overview;
- **orders the phases**: numbers, then a non-replacing refresh, then names,
  then dates *last* -- a refresh re-seeds an empty air date from the
  container's creation time -- then the metadata lock, never on a folder,
  series or season, where it reaches every item underneath.

`COPIES` and `DISCARDS` are data with a test asserting them against a fixture
server, because the failure mode of a remembered table is silence.

## `jfkit.refresh` -- ask, wait, and check what else moved

```
jfkit refresh 00000000-0000-0000-0000-000000000001 --expect Overview --apply
```

A refresh is a request, not an event: the call returns immediately and the
work is queued. Reading the record once afterwards and believing it is how a
whole batch comes back green and wrong. So a field named with `--expect` is
waited for: the refresh counts as done when that field moves or when the
record's own refresh marker (`DateLastRefreshed`, `Etag`) does, and a field
that is still what it was is printed as `expected field NAME: unchanged` and
exits non-zero.

Two guards live here and both cost real time to learn:

- **A cleared name does not come back from a provider** where the server has
  written a sidecar beside the media: the local reader runs first, sees the
  empty field, and restores the filename-derived name you were removing --
  because the server wrote that sidecar from that name. Set the name; do not
  clear it and hope.
- **Applying a searched-for identity empties the air date and production
  year**, and turns image replacement on unless the query says otherwise.
  `apply_identity()` snapshots both, applies, and writes them back in the
  phase that survives.

`jfkit notify` is the narrow nudge: tell the server that one file changed
instead of asking it to walk a library. It refuses any path that is a
library root, or above one, because that is not a notification -- it is a full
validation of everything below it. The roots are every folder of every library
the server lists, plus the two in `[paths]` and any `--root`; when the server
will not list its libraries, nothing is sent.

## `jfkit.libopts` -- the options the document leaves out

A library's options document only contains what differs from the type's
constructor defaults. Read it, change one field, send the whole object back,
and every omitted field goes back as *nothing* rather than as its default --
which is a different library, and it takes a scan to notice.

```
jfkit libopts show /var/lib/media-server/root/default/Movies/options.xml
jfkit libopts roots --data-dir /var/lib/media-server
jfkit libopts set /var/lib/media-server/root/default/Movies/options.xml \
    --id ID --field EnableEmbeddedTitles=false --backup-dir work/options --apply
```

The defaults table is data here, with a test. A write copies the document to
`--backup-dir` first (an applied `set` without one is refused), then re-reads
the document afterwards and asserts that exactly the intended fields moved.

The two record lists -- the library's paths and its per-type options, with
their fetcher lists and image options -- are parsed against their known shape
and rendered back to prove the round-trip. A document carrying anything that
shape does not cover is refused rather than sent back with a part flattened
or dropped.

`jfkit libopts roots` is the two-line check for the symptom that is hardest to
attribute: every library listed with no identifier and no options, because the
data directory moved and the route matches records by comparing paths exactly.
See `docs/runbooks/db-maintenance.md`.

## `jfkit.maintenance` -- the database, carefully

See `docs/runbooks/db-maintenance.md` for the whole procedure. The short
version: never read the live file, never write it with the service up, and no
statement that was not written down as a named operation with a precondition.

## `jfkit.swap` -- replace a file without losing its identity

See `docs/methods/swap-procedure.md`. The short version: the path does not
change, so the identity does not change; nobody is watching; the service is
stopped per chunk and the chunks are sized in bytes; every replacement is
proved to play before the first outage (`--replacement-check full|quick`); the
original is parked, never deleted; the file that arrived is read again rather
than weighed; and the verification is a comparison of the record, not of the
size.

## `jfkit.safedelete` -- evidence first

See `docs/methods/safe-deletion.md`. The short version: nothing is deletable
until somebody names a category as released; every precondition is checked
against the world rather than against the manifest; where a copy goes because
another is kept, the kept copy's payload is read and decoded first
(`--keeper-check full|quick`), and a kept copy that fails or cannot be checked
refuses the candidate; nothing is deleted, things are moved; the row goes after
the file has arrived; and every step is logged, including the ones that did
nothing.

## `jfkit.dedupe` -- copies of one film, resolved as one plan

See `docs/methods/duplicate-resolution.md`. Copies of one film are found by
provider identifier and copies of one episode by series, season and episode
plus an identifier they share -- never by a name, and never across the
segments of one episode (`S01E01a`, `S01E01b`). Every copy is probed, one
reader per disk; the keeper is the copy that may replace every other under
`[policy]` and `[policy.dedupe]`, ranked by `prefer`; and its payload is read
before anything is planned. A keeper that does not play blocks its group.

```
jfkit dedupe                                   # the dry run: verdicts and the plan
jfkit dedupe --type movie --json dedupe.json --tsv dedupe.tsv --plan-out dedupe.plan.json
jfkit dedupe --plan dedupe.plan.json --audit dedupe.audit.jsonl --apply
jfkit dedupe --plan dedupe.plan.json --audit dedupe.audit.jsonl   # where a run stopped
```

| switch | meaning |
|---|---|
| `--type movie\|episode` | only films, or only episodes (repeatable; default both) |
| `--parent ID` | only below this library, series or folder (repeatable) |
| `--keeper-check full\|quick` | how the keeper is read while planning: decoded, or every packet listed (default `policy.dedupe.keeper_check`, `full`); there is no way to skip it |
| `--parked DIR` | where parked copies go, keeping their layout; a relative folder is taken on each file's own volume (default `[paths] parked`, else `_parked` on each volume; `--apply` needs one of the first two) |
| `--json PATH`, `--tsv PATH` | also write every verdict, copy and the plan as JSON, or one row per copy as tab-separated text |
| `--plan-out PATH`, `--plan PATH`, `--audit PATH` | save the plan; apply a saved one instead of planning again; the JSON-lines audit an applied run appends to and a resumed run reads |
| `--jobs N` | server requests at once (default 4); disks are always read one reader each |
| `--no-gate`, `--gate-wait SECONDS` | do not ask the device gate before each read; how long to wait for a busy disk before giving up on a copy (default 300) |
| `--dry-run` / `--apply` | the dry run is the default; `--apply` needs `--audit` |

Each group gets one verdict, with its reasons: `SAFE` (a keeper, its payload
read), `KEEP_BOTH` (no copy may replace the others, or different cuts),
`BLOCKED` (a copy or the keeper's payload could not be read, or the keeper
failed its check) or `NOT_DUPLICATE` (segments, one file catalogued twice,
disagreeing identifiers, or episodes linked only by their slot). The plan
carries every user's watched state onto the keeper first, then parks each
other copy through `jfkit.safedelete` (category `resolved-duplicate`, which
re-checks that the state is on the keeper), then its sidecars, then release
folders left with no video, then sends one narrow notification. Exit 1 when
a group is `BLOCKED` or an applied step failed.

The library side is `find_groups(items)`, `resolve(groups, rules, prober=,
checker=, device_of=, before_each=)`, `choose(copies, rules)`,
`coverage(keeper, loser, rules)` and `build_plan(client, verdicts, parked=)`,
with `actions(client)` for `mkvkit.steps.apply`.

## `jfkit.segments` and `jfkit.jobs`

Scoping a segment analysis to media that is not already covered, and not
starting it while the disk is busy.

```
jfkit segments tasks
jfkit segments scope --plugin "segment" --backup-dir work/segments --apply
jfkit jobs gate /srv/media/movies
```

`scope` adds the covered series and films to the plugin's exclusion lists and
keeps every exclusion that was already there; the configuration as it was is
written to `--backup-dir` before the change, and an applied `scope` without
one is refused.

No plugin or task identifier is ever written down: both are looked up by
name, and a name that matches two things is an error rather than a coin toss.
An identifier in a source file addresses nothing on another machine -- and it
does not fail loudly when it does.

**The gate reports which signal made it RED.** `jfkit jobs gate PATH` looks
at eight things and names every one that holds the device:

| signal | what it sees |
|---|---|
| `reader` | a decoder or muxer whose command line names this device |
| `hidden-reader` | a decoder whose command line cannot be read -- another account's, typically the server's -- named with its owner and the server process that started it |
| `playback` | a session playing a file from this device (paused playback is a note) |
| `task` | a scheduled task the server says is running |
| `disk-activity` | the device's own counters over `--sample` seconds: more than 4 MiB/s moving, or busy more than 30 % of the time |
| `recent-changes` | items on this device the server changed within `--recent-window` minutes: previews and chapter images for changed items are made outside any scheduled task |
| `lock` | a `--lock` file that exists, quoted |
| `server` | the server could not be asked; that is not the same as idle |

The two indirect signals, `hidden-reader` and `recent-changes`, become notes
when the device's counters show it quiet over the sample: whatever they point
at is not happening on this disk. `--wait MIN` looks again every minute until
the device is clear; `--json` prints the gate as data. In Python,
`jobs.observe()` gathers the signals and `jobs.LaneGate` is a ready
`before_each` for `lanes.map_by_device`.

**A change at a library's top level costs a whole-library validation.**
`jfkit.validation.expect_library_validation(roots=..., removes=..., creates=...)`
says so beforehand: removing, parking or moving away a folder directly under
a library root, or creating a new one, makes the server refresh the library's
collection folder, which lists everything below the root again. Verbs that
remove, move or rename should print its warnings in their dry run. The roots
come from `refresh.library_roots()`.

## `jfkit.query` -- find, children, playstate

The read side a cleanup keeps needing, without an identifier up front:

```
jfkit find --name "Harbour Lights" --exact --type Series
jfkit find --path "/srv/media/series/Harbour Lights/Season 02"
jfkit find --provider Tmdb=1001
jfkit children 00000000-0000-0000-0000-000000000100 --recursive --type Episode
jfkit children 00000000-0000-0000-0000-000000000100 --extras
jfkit playstate 00000000-0000-0000-0000-000000000100 --recursive --format json
```

Every query is user-scoped: the unscoped collection route answers short and
says nothing. `playstate` reads every user by default, each through that
user's own route, with identifiers batched (`BATCH` = 50 per request) and the
requests sent `--jobs N` at a time (default 4). Output is tab-separated with
a header line, or `--format json`. `find` exits 1 when nothing matched;
`playstate` exits 1 when an item or a user could not be read, and says which.

The library side is `find(client, name=, exact=, path=, provider=, types=,
parent=)`, `children(client, item_id, recursive=, types=, extras=)` and
`playstate(client, item_ids, user_ids=None, workers=None) ->
PlayStateReport(rows, errors, missing)`, plus `render(rows, columns, fmt)`.

## `jfkit.userdata` -- watched state across a rename

A rename, a move or a renumber gives an item a new identifier, and every
user's watched state stays with the old one. Snapshot first, change the
files, then replay onto the new identifiers through a mapping (old id to new
id, as a JSON object or two tab-separated columns):

```
jfkit userdata snapshot --parent 00000000-0000-0000-0000-000000000100 --out before.json
# ... rename, notify, let the server pick the new files up ...
jfkit userdata replay before.json --map ids.tsv \
    --scope-parent 00000000-0000-0000-0000-000000000100 --plan-out replay.json
jfkit userdata replay before.json --map ids.tsv \
    --scope-parent 00000000-0000-0000-0000-000000000100 \
    --plan replay.json --audit replay.audit.jsonl --apply
jfkit userdata verify before.json --map ids.tsv \
    --scope-parent 00000000-0000-0000-0000-000000000100
```

**State handed out by slot is cleared.** After a renumber the server can give
a *new* episode the watched state recorded for its season/episode slot -- the
old file's. The replay therefore reads everything in scope, and every row the
snapshot does not account for is reset: an item no snapshot row maps onto is
expected unwatched. Widen the scope to the whole series with
`--scope-parent`; without it only the mapped items are checked.

**What cannot be cleared.** The user-data route leaves a field it is sent as
null untouched, so a last-played date can be set but never removed. A cleared
row keeps its date; the plan's notes and `verify` report those rows
separately, and they do not fail the check.

The replay is a `mkvkit.steps` plan of `userdata.write` steps: the dry run
prints it, `--plan-out` saves it, `--apply` needs `--audit`, and the same
command again resumes after a partial failure. `--apply` verifies the result
and exits 1 if a row still differs. A snapshot is refused rather than written
with a hole in it. Anybody watching during the rename is overwritten by the
replay: wait for idle first.

## `jfkit.rename` -- rename videos as one audited plan

One old-to-new pair, or a tab-separated mapping of many, with folders
allowed, becomes one plan. The plan predicts every target with the naming
port, carries the sidecars and parks each stale `.nfo`. It then snapshots
every user's watched state, renames, notifies the deepest changed folders and
waits for the new items. Last, it replays the watched state onto the new
identifiers and verifies the end state. `docs/runbooks/rename.md` is the
whole procedure.

```
jfkit rename OLD NEW [--expect SPEC]
jfkit rename --map FILE|-                 # old, new[, intention] per line
    [--park DIR] [--nfo stale|park|carry]
    [--root PATH]... [--parent ID]... [--files-only] [--allow-library-scan]
    [--refresh none|items|series] [--wait-timeout S] [--poll S]
    [--idle-timeout S] [--attempts N] [--jobs N]
    [--plan-out PATH] [--work DIR] [--dry-run|--apply]
```

| Switch | What it does |
|---|---|
| `--expect SPEC` | what one target should be read as: `S01E03`, `E03`, `S01E03-E04`, `extra`, `extra:TYPE`, `none` or `movie`. In a mapping this is the third column. Default: inferred from the new name. |
| `--map FILE` | many renames; `-` reads standard input |
| `--park DIR` | where stale `.nfo` files go, outside every library. Default: `[paths] parked`/`rename`. |
| `--nfo` | `stale` (the default) parks the document when the reading changes; `park` parks it always, `carry` never |
| `--root PATH` | a library root besides the server's and the configuration's |
| `--parent ID` | the series the items are in, instead of searching for it |
| `--files-only` | no server: predict, carry, park and rename only |
| `--allow-library-scan` | notify a root, or an unknown folder below one, anyway |
| `--refresh` | a non-replacing refresh of each new item, or of their series, once they are there |
| `--wait-timeout`, `--poll` | how long to wait for the new items (600 s) and how often to look (10 s) |
| `--idle-timeout` | how long to wait for playback to stop (0: refuse at once) |
| `--attempts N` | tries per file step when access is refused (3) |
| `--work DIR` | required with `--apply`: `rename.json`, `snapshot.json`, `plan.json`, `audit.jsonl`, `ids.json`, `report.txt`. The same folder again resumes. |
| `--plan-out PATH` | save the dry run's plan |

Exit status:

- 0 when the dry run refused nothing, or when the run was applied and
  verified;
- 1 when the plan is refused, a step failed, the server did not catch up or
  the end state differs;
- 2 for a usage error.

**Refused, with every reason printed at once:**

- a target read differently from its intention;
- a near-miss extras folder;
- a path over 259 characters with the preview tiles the server writes
  beside it;
- a collision with an existing file, among the targets, or in one season and
  episode slot;
- a file the new name would adopt as a sidecar;
- a notification that would validate a whole library.

**Chains and cycles.** They go through temporary names derived from the
mapping, so a resumed run finds the files where the first one left them.

**The library side:**

- `plan_files(pairs, Options(park=, nfo=, roots=, allow_library_scan=))
  -> FilePlan`, which only reads the disk;
- `server.prepare`, `server.wait_for`, `server.id_map` and
  `server.verify_end` for the server's part;
- `read_mapping`, `parse_expect` and `infer_expect` for the input.

---

## What is not here

- **No interactive mode, and no confirmation prompts** except the manual
  service controller's. The two states are the dry run and `--apply`.
- **No bulk metadata editor.** `jfkit item set` changes named fields on one
  item; driving it over a list is a loop, and a loop somebody wrote is a loop
  somebody can read.
- **No provider-search wrappers** beyond applying an identity you already
  have. There is no episode-level search route on this server version at all.
- **Nothing has been installed as a distribution.** No wheel has been built,
  no tag exists, nothing has been uploaded. The version is a claim about this
  tree.
