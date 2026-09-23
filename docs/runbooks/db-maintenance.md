# Runbook: looking after the catalogue database

**Implemented by `jfkit.maintenance` (`jfkit maintenance`), with service
control behind `jfkit.service`.** Confirmed against Jellyfin 12.1,
September 2026.

The database is a single SQLite file that the server holds open, in
write-ahead mode, behind a connection pool. Almost everything that goes
wrong with it goes wrong because somebody -- a script, a console, a backup
tool -- was the second program to touch it.

This runbook is four operations and the guards around them.

---

## Before anything: the two rules

**Never read the live file.** Every read here goes through a copy, or through
a connection opened read-only by the driver itself. A second reader on a
database in write-ahead mode is how an index ends up out of step with its
table.

**Never write it with the service up.** Writes go through
`jfkit maintenance run`, which insists on a service controller, stops the
service, and starts it again whether the operation worked or not.

The default controller is the manual one: it prints what to do and waits to be
told it was done. Nothing in this library decides on its own that it may stop
somebody's server.

---

## 1. Take a copy

```
jfkit maintenance snapshot /var/lib/media-server/data/catalogue.db \
    work/copies/catalogue-2026-04-03.db
```

This is the only correct way to copy a database that is open: a read-only
connection writes a consistent copy of itself. Copying the file with the
filesystem catches it mid-transaction, and copies -- or fails to copy -- the
write-ahead log separately, which is worse than not having a copy at all
because it looks like one.

An existing destination is never overwritten.

## 2. Ask it about itself

```
jfkit maintenance check work/copies/catalogue-2026-04-03.db
```

Prints the integrity answer -- `ok` is the good one -- and the row counts for
the tables worth watching. Run it on the copy. It exits non-zero when the
answer is not `ok`, so it belongs in whatever runs nightly.

## 3. Rebuild the indexes

```
jfkit maintenance run /var/lib/media-server/data/catalogue.db --reindex \
    --snapshot-dir work/copies --apply
```

The fix for a database that reads slowly and, occasionally, wrongly. It
rewrites every index from the table data, which repairs an index that has gone
out of step -- the thing a second writer leaves behind.

What the command does around it is the point:

1. asks the server whether a scheduled task is running, and **refuses** if one
   is, because stopping the service under a scan leaves it half-finished;
2. takes the copy from step 1, into `--snapshot-dir`;
3. stops the service through the controller;
4. runs the operation, commits, and truncates the write-ahead log;
5. starts the service;
6. compares the row counts before and after, and **reports any difference as
   a problem** -- a maintenance pass that changed a row count did something
   nobody asked for.

Without `--apply` it does steps 1, 2 and a description of what would run,
against the copy. That dry run is safe to run at any time, which is the whole
point of having one.

## 4. Repoint the library roots after the data directory moves

This is the one row-level fix that ships as a named operation, because it is
the one whose absence is hardest to diagnose.

**Symptom.** Every library is listed with an identifier of nothing and options
of nothing. Plugins report that a library "does not have a valid item id".
The library itself looks fine in the interface.

**Cause.** The library records keep absolute paths, and the route that lists
libraries matches each one to its record by comparing paths exactly. After the
server's data directory moves, the stored paths are stale, nothing matches,
and the route answers with the empty half of the record rather than with an
error.

**Check first**, which needs no database at all:

```
jfkit libopts roots --data-dir /var/lib/media-server
```

**Then fix it:**

```
jfkit maintenance run /var/lib/media-server/data/catalogue.db \
    --repoint /var/lib/old-location /var/lib/media-server \
    --expect-rows N --snapshot-dir work/copies --apply
```

`N` is the number of library roots your server has -- one row each, and
`jfkit libopts roots` above lists them. `--expect-rows` is not decoration. The operation refuses to write unless
exactly that many rows match the old prefix, and it refuses if any rewritten
path does not exist on disk. Both refusals happen before anything is
written. A path rewritten to somewhere that is not there produces a library
that is listed and empty, which looks like data loss and is much harder to
work backwards from than a refusal.

Note also that the prefix is matched literally: an underscore is a
single-character wildcard in this dialect, and a path containing one quietly
matches more rows than it should. The operation escapes it.

**Afterwards**, check the effective options are the ones you expect: the
server may have been reading an options document at the old location while the
copy at the new one went stale.

```
jfkit libopts show /var/lib/media-server/root/default/<library>/options.xml
```

---

## 5. Preview tiles, and the change that deletes them

**The finding.** Changing an item's type deletes the generated preview tiles
for that item *and for every item beneath it*, even when the request says not
to delete files. Queued jobs that were going to use those tiles then fail.
On a series, one type change costs every episode's tiles.

**The mitigation, which is worth more than the warning.** Keep a copy of the
tile directories before making any type change, and afterwards put back only
what is missing:

```
jfkit maintenance previews work/tile-backup /srv/media/series            # dry run
jfkit maintenance previews work/tile-backup /srv/media/series --apply
```

It is additive in both directions and deliberately not a synchronisation. A
directory that exists only where the media lives is newer than the copy and is
left alone; a file present in both is left alone; nothing is deleted on either
side. The copy is older by definition, and "newer is right" is an assumption
this has no way to check.

---

## What is not implemented

- **No vacuum, no analyse, no pragma tuning.** They are one line each in a
  database console, and they are not fixes for anything observed here. This
  module deliberately has no route for a statement somebody types once: the
  difference between a fix and a console session is whether anybody can say
  afterwards what ran.
- **No scheduling.** What runs nightly is the platform's business; see
  `docs/patterns/detached-jobs.md`.
- **The unit-manager service controller is a stub** and says so. Use the
  manual controller until there is an integration test driving a real unit.
