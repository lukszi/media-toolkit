# Runbook: renaming videos in a library

**Implemented by `jfkit.rename` (`jfkit rename`), on top of `jfkit.naming`,
`mkvkit.sidecars`, `mkvkit.steps` and `jfkit.userdata`.** Confirmed against
Jellyfin 12.1, September 2026.

A rename in a media library looks like one file operation and is eight. The
server reads the new name with its own parser; the files beside the video have
to follow it; the metadata document beside it describes the episode the server
*thought* it was; the item gets a new identifier and every user's watched
state stays on the old one; the server has to be told what changed without
being made to walk a whole library; and afterwards somebody has to look at
what it made of it. This runbook is the order those happen in, and the verb
that does them as one plan.

The same verb covers the shapes that come up:

- renumbering many episodes at once, including swaps (`A -> B`, `B -> A`) and
  chains;
- fixing one episode the server reads with the wrong numbers;
- renaming a folder, such as a misspelt extras folder whose files the server
  reads as episodes;
- renaming a video whose number is read as something it is not, such as a
  documentary whose `8000` becomes season 80.

---

## Before anything: the three rules

**The dry run is the default.** Without `--apply` nothing is renamed,
parked, sent or written. Read the whole dry run: it is exactly the plan that
`--apply` runs.

**A mismatch refuses the whole plan.** One target that the server would read
differently from what was intended, one collision, one path that is too long:
nothing moves, and every reason is printed at once.

**Keep the work folder.** `--apply` needs `--work DIR`. The folder holds the
plan, the watched-state snapshot, the audit and the report. Running the same
command again with the same folder resumes a run that stopped.

---

## 1. Write the mapping

One rename is two arguments:

```
jfkit rename "/srv/media/series/Northwind/Season 01/Northwind - S01E03.mkv" \
             "/srv/media/series/Northwind/Season 01/Northwind - S01E04.mkv"
```

Many renames are a tab-separated file, one per line: the old path, the new
path and, optionally, what the new name should be read as. A first line
naming `old` is a header; lines starting with `#` are comments.

```
old	new	expect
/srv/media/series/Northwind/Season 01/Northwind - S01E01.mkv	/srv/media/series/Northwind/Season 01/Northwind - S01E02.mkv
/srv/media/series/Northwind/Season 01/Northwind - S01E02.mkv	/srv/media/series/Northwind/Season 01/Northwind - S01E01.mkv
/srv/media/series/Harbour Lights/Season 02/E16.Golden Anchor.mkv	/srv/media/series/Harbour Lights/Season 02/E17.Golden Anchor.mkv	S02E17
/srv/media/series/Harbour Lights/Season 07/Feaaturettes	/srv/media/series/Harbour Lights/Season 07/Featurettes
```

```
jfkit rename --map renames.tsv
```

`--map -` reads the mapping from standard input. A mapping is a file rather
than arguments because a few hundred paths do not fit on one Windows command
line (the limit is 32,767 characters). Nothing in the verb builds a command
line of paths either.

**What the third column says.**

| Intention | Meaning |
|---|---|
| `S01E03` | season 1, episode 3, and no range |
| `E03` | episode 3, any season |
| `S01E03-E04` | the range 3 to 4, on purpose |
| `extra`, `extra:Featurette` | an extra (of that type) |
| `none` | a video that carries no number at all |
| `movie` | a film |
| empty, `auto` | inferred from the new name |

**Inferred intentions.** A file in an extras folder, or with an extras
ending, is meant as an extra. An `SxxEyy` token in the new name names the
season and episode. In a film library a video is a film. Anything else is
meant to carry no number. That last rule is what catches the number that is
not a number: `Signal Hill-8000 Golden Quarry (1_2).mp4` is read as season 80,
episode 0, and `8.000` in its place as season 0, episode 0 (a special). Both
are refused. Spelling the number out is not.

A name without an `SxxEyy` token that should be read as an episode, such as
`E17.Golden Anchor.mkv` in a season folder, needs its intention in the third
column. Without it, the verb refuses a name that it cannot tell was meant.

---

## 2. Read the dry run

```
jfkit rename --map renames.tsv
```

The dry run prints, in order:

1. **The prediction**: one line per video, with the intention, where it came
   from, what the naming port reads, and the old reading beneath it. Each
   line is `ok` or `REFUSED`.
2. **The parked documents**: each stale `.nfo`, and where it goes.
3. **The server's part**: how many old videos are items, and how many items
   are in scope for watched state (every video and extra of the series
   concerned). It also shows how many (item, user) rows with state would be
   carried, and across how many users.
4. **The plan**: every step, in order. Park steps come first, then any new
   folders, then the sets that step out of the way under a temporary name,
   then the direct renames, then the temporary names into place, and last
   the notifications.
5. **The refusals**, if any, every one with its reason.

Exit status 0 means nothing was refused. `--plan-out PATH` saves the plan as
JSON for review.

**What refuses a plan.**

| Refusal | Why |
|---|---|
| read differently from the intention | the item would be filed somewhere else, or become a range |
| a near-miss extras folder | its files are read as episodes, not extras |
| a path too long with the preview tiles | the tiles beside a video are the deepest path the server writes; over 259 characters many programs cannot open it. Every sidecar, every file below a renamed folder and every temporary name is checked too. |
| a target, or a sidecar's target, exists and does not move away | a rename never replaces anything |
| two renames end at one path, or one path is renamed by two lines | a path can go one place only |
| a file already beside the new name would be read as its sidecar | a left-behind `.nfo` would feed the new item another episode's metadata |
| two videos of one series in one season and episode slot | in one folder the server merges them into one item, for good |
| a rename inside a folder that is itself renamed | do them in two runs |
| another volume | a rename does not copy; move the file first |
| stale `.nfo` files and no park folder | name one with `--park` or `[paths] parked` |
| a park folder inside a library | the server would read the parked documents |
| a change that must be notified at a library root | see step 5 |
| a new folder with nothing the server knows between it and a root | see step 5 |

---

## 3. Sidecars follow, stale documents are parked

Every file that belongs to a video moves with it and keeps its tail after the
stem: pictures, subtitles, external audio, chapters, anything else named
after the video, and the `<stem>.trickplay` folder. The rules are
`mkvkit.sidecars`: a file belongs to the video with the longest stem that
claims it, and another video is never a sidecar.

**The metadata document is the exception.** The `<stem>.nfo` holds the
metadata of the episode the server *thought* the file was, and the server
reads it before any remote provider. So when the reading changes (a new
season or episode, an extra that was an episode, a number that was not
meant), the document is parked instead of renamed. It goes to
`<park>/rename-<digest>/<nnnn>-<name>`, where the digest is the mapping's.

- `--nfo stale` is the default.
- `--nfo park` parks every document.
- `--nfo carry` parks none.

A park folder on another volume is reached by a verified copy.

The new item gets a new *added* date: it is a new item, and with the document
parked nothing carries the old one.

---

## 4. Apply

```
jfkit rename --map renames.tsv --apply --work /srv/staging/rename-northwind
```

In order:

1. **Nobody is watching.** A replay overwrites anything changed during the
   rename. With playback in progress the run refuses. `--idle-timeout S`
   waits for it to stop instead.
2. **Snapshot.** Every user's watched state for every item in scope is read
   and saved to `snapshot.json` before anything moves. A snapshot with a
   hole in it is refused, not written.
3. **The steps.** Each file step is a `mkvkit.steps` action. A rename never
   replaces anything. A file whose old name is held open by another program
   is retried (`--attempts N`, default 3, with a doubling pause); if it still
   fails, the step fails whole and the run stops. Every step goes into
   `audit.jsonl` as it runs.
4. **Cycles and chains.** A set whose target is held by another set of the
   same plan first steps out of the way under the old name plus
   `.rename-<digest>`. No parser reads that name as a video. It moves into
   place after everything else.

### When it stops half way

Run the same command again with the same `--work`. The mapping may be left
out. Steps the audit records as done are skipped; a step whose effect is
already in place is recorded as done without running. The saved snapshot is
used, because the old items may already be gone. A work folder belongs to
one mapping: a different mapping is refused.

---

## 5. Notify, and only as deep as needed

The server is told about the deepest folders whose contents changed: the
folder each video left and the folder it arrived in, in batches of 100
paths. Nothing wider is sent:

- **A library root is refused.** A notification naming a root validates the
  whole library. A rename that can only be notified at a root, such as
  renaming a series folder that sits directly in the root, is refused unless
  `--allow-library-scan` is given.
- **A new folder that the server does not know is refused too.** The server
  walks up from a path it does not know to the nearest item it does. For a
  new series folder, that item is the library. `--allow-library-scan`
  accepts that as well.

Without the list of the server's library folders the run refuses. It never
takes that list as empty.

---

## 6. Wait, and refresh if asked

A notification is a request. The run reads the catalogue again every
`--poll` seconds (default 10) until every new path is an item and no old path
still is, or until `--wait-timeout` (default 600) passes. After a timeout it
exits 1; the same command again waits again, and the renames are not
repeated.

`--refresh items` queues a non-replacing refresh of each new item, and
`--refresh series` one of each series. The default is no refresh.

---

## 7. Replay the watched state

The old identifiers map to the new ones through the paths
(`ids.json`). The replay is `jfkit.userdata`: it writes each snapshot row onto
its new identifier, and it clears every row in scope that the snapshot does
not account for. That includes the state the server hands a new episode for
the season/episode slot an old file used to hold (gotcha 5.6). Then it
verifies.

A cleared row keeps its last-played date, because the route cannot remove
one. That row is reported, and it does not fail the run.

---

## 8. Check the end state

For every target, the report (printed, and saved as `report.txt`) says what
the server made of it:

- an episode has the intended season, episode and end number;
- an extra is listed among the extras;
- a video meant to carry no number carries none;
- no item still has an old path;
- no path is held by two items, and no season and episode slot of a moved
  episode is held twice.

Exit status 0 means the replay verified and every target is as intended.

---

## Without a server

`--files-only` predicts, carries, parks and renames, with the same checks and
the same audit and resume. It sends nothing and carries no watched state.
Library roots then come from `[paths]` and `--root`.

---

## What is not implemented

- **No move between volumes.** A rename stays on one volume; a verified move
  is `mkvkit steps` with a `move` step.
- **No sidecar editing.** A parked document is not rewritten. Gotcha 3.2
  describes stripping one instead, for a document worth keeping.
- **No renaming a folder and its contents in one run.**
- **Paths are compared as the server spells them.** A server that sees the
  library under another mount point than this machine does is not supported.
- **The added date is not carried.** A new item is new.
