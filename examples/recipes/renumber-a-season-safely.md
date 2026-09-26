# Recipe: renumber a season that the server read wrong

Everything here is invented: the series, the paths, the identifiers and the
numbers. It is a worked example of the shape of a job, not a record of one.

**The job.** `Harbour Lights` season 1 was ripped with filenames like
`Harbour Lights 1-05 Pilot.avi`. The server read `1-05` as *episodes 1 to 5*,
so five episodes collapsed into one item, four more are missing, and the one
that is there has the wrong overview. We want season 1 numbered correctly,
with the right names, and nothing else touched.

**What makes this hard.** The number range is not writable through the API and
cannot be cleared durably: it is refilled from the path on any refresh that
runs a provider. A rename is the only fix, and the order of everything after
the rename decides whether it survives the next refresh.

---

## 1. Ask what the filenames will be read as, before touching them

```
jfkit naming "/srv/media/series/Harbour Lights" --only-problems
```

```
RANGE S=- E=1 end=5 [absolute]  Harbour Lights 1-05 Pilot.avi
        note: read as an absolute episode number; adding an SxxEyy token to the name suppresses this expression
        note: this name produces an episode range; the end number is refilled from the path on any refresh that runs a provider, so only a rename clears it
        note: the parent folder names season 1; the expressions never read it, but the server does
RANGE S=- E=6 end=7 [absolute]  Harbour Lights 6-07 Low Water.avi
        note: read as an absolute episode number; adding an SxxEyy token to the name suppresses this expression
        note: this name produces an episode range; the end number is refilled from the path on any refresh that runs a provider, so only a rename clears it
        note: the parent folder names season 1; the expressions never read it, but the server does

11 path(s), 2 shown, 2 would be read as a range, 0 claimed by no expression.
Checked against Jellyfin 12.1, September 2026; see docs/gotchas/jellyfin-12.md.
```

The path is quoted because it has a space in it: unquoted, the shell hands
the command two paths, neither of which exists.

Two names produce ranges. That is the whole diagnosis, and it took one
command that read nothing but filenames. It exits non-zero, so it can sit in
front of a rename in a script.

## 2. Check the sidecars before renaming, not after

This is the step that is skipped and then costs an afternoon.

Wherever the server has written a description file beside a media file, that
file carries the numbers, dates, provider identifiers and plot **of the
episode the server thought it was**. Renaming the media file does not fix it:
the local reader runs first on the next refresh and fills the empty fields
back in from the wrong episode.

```
ls "/srv/media/series/Harbour Lights/Season 01/"
```

```
Harbour Lights 1-05 Pilot.avi
Harbour Lights 1-05 Pilot.nfo      <- written by the server, holds 1-05
```

Either remove the description file, or strip the fields it should not be
asserting -- the air date, the year, the provider identifiers, the cast, the
plot and the end-episode number -- and keep only the date it was added.

## 3. Rename

```
mv "/srv/media/series/Harbour Lights/Season 01/Harbour Lights 1-05 Pilot.avi" \
   "/srv/media/series/Harbour Lights/Season 01/Harbour Lights S01E01 Pilot.avi"
```

The extension stays `.avi`. A rename changes what the file is called, not what
is inside it; calling an AVI file `.mkv` does not make it Matroska, and every
tool that trusts the extension is then wrong about it.

Then confirm the prediction agrees with what you meant:

```
jfkit naming "/srv/media/series/Harbour Lights/Season 01/Harbour Lights S01E01 Pilot.avi"
```

```
      S=1 E=1 end=- [season-episode]  Harbour Lights S01E01 Pilot.avi

1 path(s), 1 shown, 0 would be read as a range, 0 claimed by no expression.
Checked against Jellyfin 12.1, September 2026; see docs/gotchas/jellyfin-12.md.
```

`Harbour Lights 6-07 Low Water.avi` gets the same treatment, as episode 6.

## 4. Tell the server about the folder -- and only the folder

```
jfkit notify "/srv/media/series/Harbour Lights/Season 01" --apply
```

Naming the *library root* here would not be a notification. It would be a full
validation of everything below it, which on a large collection is hours of
disk, and it is how an accidental full scan starts. The command refuses any
path that is a library folder the server lists, or above one:

```
$ jfkit notify /srv/media/series --apply
/srv/media/series is a library root, or contains one: notifying it starts a
full validation of everything below it. Name the file that changed, or the
deepest folder that did.
```

## 5. Fix what the rename did not

The rename fixes the numbers. The names, where they were derived from the
filenames, are still wrong:

```
jfkit survey metadata --format md | head -20
```

```
| Type    | Name                                  | Name is   | Missing        |
|---------|---------------------------------------|-----------|----------------|
| Episode | Harbour Lights 1-05 Pilot             | untitled  | overview,date  |
```

Now the ordering rule, which is the whole point of `jfkit item set`:

```
jfkit item set 00000000-0000-0000-0000-000000000011 \
    --field IndexNumber=1 --field ParentIndexNumber=1 \
    --field Name=Pilot \
    --field PremiereDate=1998-04-03 \
    --backup work/before/episode-11.json --apply
```

```
  numbers: IndexNumber, ParentIndexNumber
  refresh (non-replacing)
  names: Name
  dates: PremiereDate
applied
```

Four phases, in that order, for three separate reasons:

- **numbers first**, so the refresh that follows is working on an item the
  provider can match;
- **a non-replacing refresh next**, so the provider fills in the overview and
  the images -- replacing would discard the corrections;
- **names after the refresh**, because a library configured to believe the
  container's embedded title overwrites the name *during* a refresh;
- **dates last**, because the probe that runs at the front of every refresh
  re-seeds an empty air date from the container's creation time. A date
  written before a refresh is a date that will not survive it.

Note also what was **not** done: the name was set, not cleared. The recipe
"clear the name and let a provider fill it in" fails wherever a description
file sits beside the media, because the local reader runs first and restores
the name you were removing.

## 6. Check that nothing else moved

```
jfkit item diff 00000000-0000-0000-0000-000000000011 work/before/episode-11.json \
    --expect Name --expect IndexNumber --expect ParentIndexNumber \
    --expect PremiereDate --expect Overview
```

```
00000000-0000-0000-0000-000000000011: no drift
  expected: Name: 'Harbour Lights 1-05 Pilot' -> 'Pilot'
  expected: IndexNumber: None -> 1
  expected: ParentIndexNumber: None -> 1
  expected: PremiereDate: None -> '1998-04-03T00:00:00.0000000Z'
  expected: Overview: None -> 'An invented description of Pilot.'
```

Every change is one that was declared. `no drift` means nothing else moved --
not the provider identifiers, not the poster, not the play state, not the
identifier.

## 7. When it is right, lock it

```
jfkit item set 00000000-0000-0000-0000-000000000011 --field LockData=true \
    --backup work/before/episode-11-lock.json --apply
```

The lock is what makes the numbers survive a replacing refresh or a
re-identification later.

**Never on a folder, a series or a season.** It reaches every item underneath,
and nothing undoes that in one call. `jfkit item set` refuses:

```
$ jfkit item set 00000000-0000-0000-0000-000000000010 --field LockData=true \
    --backup work/before/season-01.json --apply
refusing to write ['LockData'] on a Season: the lock reaches every item
underneath it, and nothing undoes that in one call
```

---

## Afterwards

```
jfkit survey filename-parse --out work/surveys
```

gives one row per episode path with the predicted numbers and whether they
agree with the catalogue, which is how you find the next twelve of these
before somebody reports them.
