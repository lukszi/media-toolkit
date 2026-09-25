# Jellyfin 12.x: things that cost a day

Every entry carries the **symptom**, the **cause** and the **fix**. These are
empirical findings about one release, not documentation, and they go stale.
Where an entry is about a bug rather than a design decision, it says so.

**Confirmed against Jellyfin 12.1.0, September 2026**, on a single
server with one administrative account. That version applies to every
entry below. An entry carrying a **Confirmed** line was reproduced
deliberately, and the line is the experiment; the shorter entries without one
record a behaviour met in the course of the work and read from the
implementation afterwards, which is weaker evidence and is why they are
shorter.

Every example uses an invented title, an invented path and the all-zero
identifier shape (`00000000-0000-0000-0000-00000000000N`). `jfkit` implements
the safe form of several of these; the module is named where it does.

Contents: [writing an item back](#1-writing-an-item-back) ·
[refresh semantics](#2-refresh-semantics) ·
[names, numbers and files](#3-names-numbers-and-files) ·
[what triggers a scan](#4-what-triggers-a-scan) ·
[identity, ids and user data](#5-identity-ids-and-user-data) ·
[search and provider routes](#6-search-and-provider-routes) ·
[the database and the data directory](#7-the-database-and-the-data-directory) ·
[scheduled tasks and segments](#8-scheduled-tasks-and-segments) ·
[what the API can and cannot do](#9-what-the-api-can-and-cannot-do)

---

## 1. Writing an item back

### 1.1 A field you leave out of the update is not left alone -- it is cleared

**Symptom.** You update one field through the item-update route and unrelated
fields come back empty: the overview, the studios, the sort name.

**Cause.** The update handler assigns scalars unconditionally from the body it
receives. There is no "unset means unchanged".

**Fix.** Read the full single-item record, change the fields you mean to
change in that object, and post the whole thing back. `jfkit` does this in one
place so no caller has to remember; a partial write is not offered as an
option.

**Confirmed.** Reproduced deliberately on a test item: posting a body with
only the name cleared the overview and the sort name.

### 1.2 The list route does not return every field the single-item route does

**Symptom.** A field read from a collection query is always absent or always
false -- the metadata-lock flag is the one that bites -- so a tool concludes
nothing is locked and helpfully unlocks it.

**Cause.** List queries serialise a narrower shape than the single-item route,
whatever `fields` you ask for.

**Fix.** Read the single item before writing it. Never round-trip a record
that came out of a list query.

**Confirmed.** The lock flag is absent from the list shape and present on the
single item, on the same item, in the same session.

### 1.3 Strip the preview-image block before posting

**Symptom.** The update route answers 500, with nothing useful in the body.

**Cause.** The record carries a trickplay/preview-image block that the update
handler cannot deserialise back.

**Fix.** Remove that key from the object before posting. It is not something
you can set through this route anyway.

**Confirmed.** Reproducible on any item that has preview images: identical
request, minus that key, succeeds.

### 1.4 Order the phases of a repair: numbers, refresh, names, dates, lock

**Symptom.** You fix an episode's numbers and its title, refresh, and the
title is wrong again -- or the air date has moved.

**Cause.** Several providers run during a refresh, and two of them write
fields a person just set (see 2.1 and 3.4). The order in which you write and
refresh decides who wins.

**Fix.** The order that survives: set the numbers → non-replacing refresh →
set the names and overview → set the dates **last** → set the metadata lock
last of all, and never on a folder, a series or a season (see 1.5).

**Confirmed.** Used as the recipe on several separate repairs; each step's output
was re-read afterwards.

### 1.5 The metadata lock cascades down a folder, a series and a season

**Symptom.** Locking one container silently locks every episode beneath it,
and none of them accepts provider data again.

**Cause.** The flag is applied recursively to children.

**Fix.** Set it on leaf items only. If you need a series-wide freeze, set it
per episode, and record that you did -- because the inverse operation is just
as recursive.

**Confirmed.** Set on one season; every episode beneath it came back locked.

### 1.6 The lock is the only durable fix for some fields, and it costs you every provider

**Symptom.** A field you cleared comes back on the next refresh, whatever you
do.

**Cause.** The refresh pipeline fills some fields from the path and the
container before any provider runs (see 3.3, 3.4), and the only thing that
suppresses that is the item being locked.

**Fix.** Know what you are buying. A locked item receives no titles,
overviews, dates or images again, from any provider, ever -- until it is
unlocked. For a renumbering problem, renaming the file is nearly always the
better trade.

**Confirmed.** A cleared end-number stayed cleared only while the item was
locked, and the same item then stopped receiving a title from the provider.

---

## 2. Refresh semantics

### 2.1 A refresh re-seeds dates and the overview from the container, before any provider

**Symptom.** An item you have just corrected acquires a premiere date in the
wrong year, or an overview that reads like a download banner.

**Cause.** A probe-based provider runs first and fills empty fields from
container metadata: the creation time becomes the premiere date and the
production year; a container comment tag becomes the overview.

**Fix.** Write dates last (1.4). For the overview, delete the offending tag
from the container -- a header-only edit, no remux (`mkvkit.propedit`, and
see `matroska-ffmpeg.md`) -- then clear the field and refresh.

**Confirmed.** Files carrying a distributor banner in the container
comment tag; after the header edit and one non-replacing refresh, the field
stayed as set.

### 2.2 "Replace all" and "identify" are not repairs

**Symptom.** An identify run on a damaged item destroys more than it fixes:
local images disappear, the premiere date and production year are nulled, and
by-date episodes lose their numbers.

**Cause.** Replace-all semantics. Two specifics worth knowing: the apply route
defaults to replacing images, and under replace-all with a metadata saver
enabled, local providers are skipped entirely -- so a sidecar is rewritten
without the numbers it used to carry.

**Fix.** Pass the "do not replace images" flag explicitly on every apply. Use
a non-replacing full refresh for repairs. If you must identify, do it
**before** any hand edits, and back up the user data first (5.4).

**Confirmed.** Reproduced on a test item with local images and a hand-set
date.

### 2.3 Changing the display order of a series queues a replace-all refresh

**Symptom.** A one-field change to a series is followed by a long, unexplained
refresh of everything under it.

**Cause.** That field is treated as a change of identity for ordering, and the
handler queues a replace-all.

**Fix.** Expect it, and do not schedule it beside other work on the same
disk. If the series has hand-set numbers, lock them first or re-apply
afterwards.

**Confirmed.** Observed more than once; the queued task appears immediately after the
update.

### 2.4 A refresh is queued, not immediate

**Symptom.** You refresh, read the item back, and see the old data; a minute
later it is right.

**Cause.** The refresh is a queued job, and how long it takes depends on what
else is running.

**Fix.** Poll the item until the field you expect actually changes, with a
timeout and a bounded number of attempts -- never a fixed sleep. `jfkit`'s
refresh helper does this and diffs everything outside the fields you declared
you were changing, so a refresh that moves something else is reported rather
than silently accepted.

**Confirmed.** A few polls a few seconds apart were typical on an idle
server; a busy one took considerably longer.

---

## 3. Names, numbers and files

### 3.1 A sidecar file re-fills a field you cleared, before any remote provider can

**Symptom.** The standard recipe -- clear the name, run a non-replacing
refresh, let the provider supply the real title -- fails wherever a sidecar
sits next to the media file. The name comes back exactly as it was.

**Cause.** The local sidecar provider runs first, sees an empty name, and
restores its own title: which is the filename-derived string you were trying
to remove, because the server wrote that sidecar from the bad name in the
first place.

**Fix.** Set the name explicitly through the item-update round-trip instead of
clearing it and hoping. The server then rewrites the sidecar with the good
title.

**Confirmed.** Cleared, refreshed, and the name returned verbatim -- while the
overview from the remote provider *did* arrive in the same refresh, proving
the provider had run. Where no title survived in the sidecar, the clear-and-
refresh trick still worked.

### 3.2 A sidecar written while an item was misnumbered is poisoned

**Symptom.** You rename a misparsed file, the numbers are right, and the item
still carries another episode's air date, cast, plot and provider ids.

**Cause.** Every sidecar the server wrote while the file was misnumbered
carries the *wrong* episode's data. The sidecar is a local provider that runs
before every remote one and fills any field that is empty, so a surviving air
date pins the item for ever.

**Fix.** Strip the poisoned elements from the sidecar **before** renaming:
the air date, the year, the rating, every provider id, the cast and crew, the
plot, the end-number, and any artwork paths (which point at the old item's
metadata folder). Keep the added date -- a rename mints a new item id, and
without it everything resurfaces as recently added.

**Confirmed.** Every sidecar in one affected season carried the same wrong
air date and the same wrong provider id.

### 3.3 An end-number cannot be written, and cannot be cleared durably

**Symptom.** A file whose name parses as an episode *range* gets an end
number. The update route accepts your attempt to clear it and changes nothing.

**Cause.** The field is not writable through the API, and the refresh pipeline
re-derives it from the path on any refresh that runs a provider. There is a
transient clear -- posting a start number greater than the end number nulls it
during save -- but the next provider refresh fills it in again, because the
save hook runs before the path-derived fill.

**Fix.** Rename the file. Adding a season-and-episode marker to the name is
the durable cure, because the expression that produces the range is guarded by
a negative lookahead for exactly that marker. `jfkit.naming` predicts the
parse before you rename, and its fixture table pins the behaviour.

**Confirmed.** The update route answered 204 with the value unchanged; the
transient clear was reproduced and then observed to refill on the next
refresh.

**Why it matters more than it looks.** A metadata provider walks the whole
range and concatenates the title and plot of *every* episode in it, on every
refresh. That is where "Title A / Title B / Title C" names come from.

### 3.4 A date in a filename makes an item by-date, and by-date items lose their numbers

**Symptom.** Episode numbers written through the API vanish on the next
refresh, whatever refresh mode you use.

**Cause.** A filename carrying a date resolves as a by-date episode, and the
numbers are re-derived from the path every time.

**Fix.** Rename the date out of the name and give it a season-and-episode
marker, or lock the item (1.6). The rename is better unless the item needs
nothing further from any provider.

**Confirmed.** Reproduced on a file renamed in both directions.

### 3.5 Files in one folder that parse to the same numbers are merged, irreversibly

**Symptom.** Half a series is missing from the library, and the episodes that
are missing are the ones that exist twice on disk.

**Cause.** The resolver groups files in one folder that parse to the same
season and episode into one primary item plus hidden alternate-version
children, on every validation, gated by a flag that is not configurable. The
hidden children are not returned by recursive-children queries, so no virtual
season is created for their numbers either.

**Fix.** A filename change is the only cure. The route that removes alternate
sources removes *manual* links only; renumbering through the API does not
un-hide anything.

**Confirmed.** A database count and an API count of the same library differed,
and the difference was exactly the hidden children.

### 3.6 Container twins share one sidecar

**Symptom.** You number one file through the API and its differently-
containerised twin acquires the same numbers -- and the same metadata lock --
on the next refresh.

**Cause.** `Blue Canyon.mkv` and `Blue Canyon.mp4` both read and write
`Blue Canyon.nfo`.

**Fix.** Revert the twin afterwards, or remove it. Do not assume a per-item
write is per-file.

**Confirmed.** Every twin checked picked up the numbers written to its
sibling.

### 3.7 A season folder needs a season keyword

**Symptom.** After an upgrade, folders that used to yield a season number
produce absurd ones -- a season in the two thousands, matching a year in the
folder name.

**Cause.** The season-path parser now requires a keyword (`Season 01`, `S01`,
and the localised equivalents). A folder named `01 - something` no longer
matches, so the name is handed to the episode expressions instead, which find
a number somewhere and use it.

**Fix.** Name season folders with the keyword. Check with
`jfkit.naming.parse`, which reports the folder's contribution separately from
the filename's.

**Confirmed.** The same folder names produced a season with the keyword and
nothing, or nonsense, without it.

### 3.8 An episode renumbered out of its physical season folder is listed twice

**Symptom.** One episode appears under two seasons.

**Cause.** The display rule matches an episode to a season if the numbers
agree **or** if its physical season shares a presentation key. Both can be
true at once.

**Fix.** Move the file, or accept the duplicate. Renumbering alone does not
resolve it.

**Confirmed.** Reproduced by renumbering an episode across seasons without
moving it.

### 3.9 Only fourteen directory names are recognised as extras

**Symptom.** A folder of bonus material resolves as a *season* named after the
folder, full of episode rows -- one of which may acquire an end number from a
clip's filename.

**Cause.** The extras classification is by exact directory name, from a fixed
list (trailers, backdrops, theme-music, behind the scenes, deleted scenes,
interviews, scenes, samples, shorts, featurettes, extras, extra, other,
clips). Anything else is a season.

**Fix.** Rename the directory to one of the recognised names.

**Confirmed.** Each differently-named bonus folder became a fake season.

### 3.10 An empty ignore file ignores the whole directory

**Symptom.** You add an ignore file to exclude one file and the entire folder
disappears.

**Cause.** An empty ignore file means "ignore this directory". To exclude one
file, write its name as a line -- and avoid patterns that also match the
parent folder's name.

**Fix.** As above. To make the server forget rows under an already-ignored
folder, send a notification naming the **parent** folder, not the ignored one.

**Confirmed.** Both behaviours reproduced.

### 3.11 A newly written ignore file is not honoured until a restart

**Symptom.** The ignore file is correct, the folder is re-scanned, and nothing
changes.

**Cause.** The rule negatively caches "there is no ignore file here" per
directory, and the cache-clearing call clears one instance while resolution
uses another. Folder notifications and default refreshes are no-ops for it.

**Fix.** Restart the service, or wait for the next full scan.

**Confirmed.** Reproduced; a restart resolved it immediately. **This looks
like a bug worth reporting upstream.**

---

## 4. What triggers a scan

### 4.1 A media-updated notification with a library root path starts a full validation

**Symptom.** A tool that meant to re-resolve one folder starts a scan of the
whole library, which then holds the disks for hours.

**Cause.** The notification route treats a library root as "validate
everything beneath this".

**Fix.** Always pass the deepest folder that contains the change, or the file
itself. `jfkit` refuses a path that equals a library root.

**Confirmed.** This is how one unplanned full scan started.

### 4.2 The realtime monitor reacts to anything you write next to a media file

**Symptom.** Writing a sidecar triggers a validation of its folder about a
minute later, which can trigger other entries in this document.

**Cause.** The realtime file monitor, with a short debounce.

**Fix.** For a batch of file operations, stop the service, do the work, and
start it again. Otherwise stage the files elsewhere and move them into place
in one operation per folder.

**Confirmed.** Observed on every sidecar write.

### 4.3 Any change to a file's modification time costs a re-extraction

**Symptom.** A header-only edit -- which writes a few kilobytes and changes no
packet -- is followed by hours of processing on the same disk.

**Cause.** Extracted chapter images are named after the item's modification
time, so any change invalidates all of them, and preview-image tiles are
regenerated on the same trigger. Both run on demand, outside any scheduled
task.

**Fix.** Fold every pending edit to a file into a single write. Do not
schedule a heavy read on a disk that is being re-extracted; check for the
server's own probe processes first. `mkvkit`'s job runner gates on this.

**Confirmed.** A short multi-window audio read of one file slowed by more
than an order of magnitude while a re-extraction held the same disk.

---

## 5. Identity, ids and user data

### 5.1 An item id is derived from its path

**Symptom.** Renaming a file produces a new item, and the old one disappears
along with anything attached to it.

**Cause.** The id is a hash of the item type and the file path.

**Fix.** Swap files **in place**, under the same path, whenever you can: that
is what preserves the identity, the watch state and everything pointing at it.
When you must rename, plan for the identity change and carry the user data
across deliberately (5.4).

**Confirmed.** Renames were verified in advance to affect only items with no
user data.

### 5.2 Ids are spelled two ways

The database stores them upper-case with dashes; the API takes and returns
them lower-case without. Compare them normalised, or a join silently produces
nothing.

### 5.3 Watch state re-attaches at the same path, but not across a rename

**Symptom.** Deleting and recreating an item at the same path keeps the watch
state. Doing the same across a rename, or after an identify, loses it.

**Cause.** The user-data row is keyed by a derived key, which survives a
delete and recreate at the same path and changes when the path or the
identity changes.

**Fix.** Back the user data up before anything that changes identity, and
restore it explicitly afterwards.

**Confirmed.** Both directions reproduced.

### 5.4 Back up user data across **every** account, not just yours

Watch state, favourites and resume positions belong to each account
separately. A restore that only covers the administrative account looks
complete and is not. This is also a privacy matter: the fewer of those rows
you handle, the better. `jfkit delete` and `jfkit swap` check every account
the server lists (`GET /Users`) unless told to check named ones, and refuse
to apply when that list cannot be read or comes back empty -- "0 users
checked" is nobody having been asked, not a pass.

### 5.5 Extras attach by owner, and detach

**Symptom.** Extras stop being listed under their film.

**Cause.** The link is an owner id on the extra, and it can be lost.

**Fix.** A folder notification naming the **film's own file** re-attaches the
extras in that folder in place. A full scan does the same for everything, at
much greater cost.

**Confirmed.** Detached extras re-attached from folder notifications alone,
with no full scan.

---

## 6. Search and provider routes

### 6.1 The unscoped collection query silently omits items

**Symptom.** An audit built on a recursive collection query comes up short,
with no error and no marker. Items that are missing can still
be found by a search term.

**Cause.** The unscoped route applies a narrower resolution than the
user-scoped one.

**Fix.** Query user-scoped -- always. `jfkit.client.items()` is user-scoped by
default and warns when asked for the unscoped route. **This looks like a bug
worth reporting upstream.**

**Confirmed.** The unscoped query came back short and the user-scoped query
returned everything, in the same session. The share omitted is not a
constant -- it depends on what is in the library and on who is asking -- so
compare the two counts on your own library rather than assuming either.

### 6.2 The unscoped single-item route refuses

`GET /Items/{id}` answers 400. Use `GET /Users/{userId}/Items/{id}`. This one
at least fails loudly.

### 6.3 There is no remote-search route for episodes

**Symptom.** A 404 from the episode remote-search route, which the
documentation implies should exist.

**Cause.** The route is not implemented; the published interface description
lists the other item types and not this one.

**Fix.** Episode-level provider data can only be obtained through a refresh.
The **series** remote-search route does accept a bare set of provider ids with
no name and resolves them, which is enough to identify an unknown series.

**Confirmed.** 404 on the episode route; the series route resolved a series
from a provider id alone.

### 6.4 An empty remote-image list is a diagnosis, not a failure

If the remote-images route reports zero results for an item, the item's
identity gives the image providers nothing to work with -- typically a
library-type mismatch rather than a refresh problem. It is a one-call proof
and worth trying before anything expensive.

---

## 7. The database and the data directory

### 7.1 Never query the live database file

**Symptom.** A read-only query against the live database returns stale rows,
or locks something, or both.

**Cause.** Write-ahead logging plus a connection pool. Options in the server's
configuration that look like they disable locking operate at a different
layer.

**Fix.** Copy it with the database engine's own snapshot statement -- a plain
file copy of a write-ahead-logged database is not a consistent snapshot -- and
query the copy. Do it while the service is stopped for anything that matters.

**Confirmed.** Used for every survey in this project; the snapshot is also the
rollback.

### 7.2 After moving the data directory, library roots keep their old paths

**Symptom.** Every library comes back from the virtual-folder route with no
item id and no options; plugins that resolve a library by id skip all of them;
the server reads and writes the effective options file at the *old* location
while the copies at the new one go stale.

**Cause.** The root and collection-folder rows still hold the old absolute
paths, and the virtual-folder lookup matches a folder to its row by **exact
path**. Moving the data directory rewrites one of those rows and not the rest.

**Fix.** Stop the service, snapshot the database (7.1), rewrite the paths on
the root and collection-folder rows to the new location, restart, and verify
that every library reports an item id and its options again.

**Item ids are not at risk**, which is the one piece of good news: the id is
derived from the path *relative to the data directory*, so it is identical in
both locations.

**Confirmed.** One row per library root; libraries, view ids and item counts
identical afterwards. **This looks like a bug worth reporting upstream.**

### 7.3 Changing an item's type deletes its preview images -- and its children's

**Symptom.** A type change on a container is followed by every episode
beneath it losing its preview tiles, and by queued preview jobs failing.

**Cause.** The type change deletes and recreates the item, and the delete path
removes external files for the item and every recursive child -- including
when file deletion is configured off.

**Fix.** Back the tiles up first. They can be restored additively afterwards,
and the built-in generation task imports existing tiles rather than
regenerating them. `jfkit.maintenance` does the backup and the additive
restore; it never overwrites and never deletes.

**Confirmed.** Reproduced on one series; the restore brought every item back.

---

## 8. Scheduled tasks and segments

### 8.1 Look up plugin and task identifiers, never hard-code them

A plugin instance id or a task id copied into a script is a latent silent
failure: on another server, or after a reinstall, the call addresses nothing
and reports success. Resolve them by name through the plugins and
scheduled-tasks routes at run time.

### 8.2 A segment-database plugin can fail nightly and succeed by day

**Symptom.** A plugin that downloads media segments reports success and
imports nothing, run after run, at its scheduled hour.

**Cause.** The upstream service it reads returned deployment timeouts and
server errors at that hour on consecutive nights. Run by hand at mid-morning,
the same task completed in seconds and imported what the nightly runs had not.

**Fix.** Move the trigger to a daylight hour through the scheduled-tasks
route, and treat "task completed" as distinct from "task imported anything":
check the count.

**Confirmed.** Failed nightly runs, a successful manual run, and a coverage
measurement before and after.

### 8.3 Segment detection may skip every library after a data-directory move

If a segment-detection plugin logs that a library has no valid item id and
then that no libraries are selected, check 7.2 before changing any plugin
setting: the libraries are unresolvable, not unselected.

### 8.4 Automatic segment detection is a full audio pass over the library

Turning it on is not a setting change, it is a scheduled fingerprint of every
episode. Scope it to the material a downloaded database does not cover,
exclude the rest, and run it once on an idle disk.

---

## 9. What the API can and cannot do

Measured against 12.1 by round-tripping a full item record and diffing.

**Copied by the item update:** name, sort name, original title, original
language, ratings, index number, parent index number (on any type, including a
season), overview, genres, the first tagline, studios, people, the dates,
official and custom ratings, tags, production locations, locked fields, the
metadata lock (cascades -- see 1.5), provider ids (an empty object clears
them), preferred metadata language and country, display order (queues a
replace-all -- see 2.3), aspect ratio, the 3D format, an episode's
airs-after/airs-before numbers, the transient series name, and a series'
runtime, status, air days and air time.

**Discarded outright:** path, extra type, owner id, parent id, season id,
**index number end**, width, channel number.

| Goal | Verdict |
|---|---|
| Fix episode and season numbers | Yes -- durable except under replace-all or identify; the metadata lock immunises, at the cost in 1.6 |
| Merge two physical seasons into one | Yes -- set the season's index number and refresh |
| Split a flat folder into series | Yes -- set the content type and re-scan; note 7.3, the type change wipes preview images |
| Un-merge alternate versions | **No** -- filename only (3.5) |
| Clear an end number | **No** -- rename only (3.3) |
| Make a video an extra | **No** -- folder and filename only (3.9) |
| Re-identify an item | Yes -- with images explicitly not replaced, and **before** any hand edits (2.2) |
| Restore watch state after a path change | Yes -- write the user data back per account (5.3, 5.4) |
| Restore a remembered audio or subtitle choice | Yes -- through a playback-progress report on a user-bound token; the indices are absolute |
| Re-resolve one folder without a scan | Yes -- a media-updated notification naming the deepest path, or a default refresh of the folder item (4.1) |
| Re-attach detached extras | Yes -- a folder notification naming the film's own file (5.5) |

---

## Reporting these upstream

Three entries here look like bugs rather than decisions, and are worth filing
with the project rather than only being written down: the unscoped collection
query omitting items (6.1), library roots keeping their old paths after a
data-directory move (7.2), and the ignore-file cache that only clears on
restart (3.11). This document is not a substitute for reporting them.
