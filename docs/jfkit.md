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
```

`naming` and `survey` read and change nothing, and so does `item` except
for `item set`, which writes. `naming`, `survey`, `item show` and
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

Exits non-zero when any name would be read as a range. It is pinned by a
table of sixty-five invented names and records the release it was read
against, because a regex port of somebody else's parser with no fixtures will
eventually be confidently wrong.

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
stopped per chunk and the chunks are sized in bytes; the original is parked,
never deleted; the file that arrived is read again rather than weighed; and
the verification is a comparison of the record, not of the size.

## `jfkit.safedelete` -- evidence first

See `docs/methods/safe-deletion.md`. The short version: nothing is deletable
until somebody names a category as released; every precondition is checked
against the world rather than against the manifest; nothing is deleted, things
are moved; the row goes after the file has arrived; and every step is logged,
including the ones that did nothing.

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
