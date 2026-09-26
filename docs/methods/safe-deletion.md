# Evidence-first deletion

**Implemented by `jfkit.safedelete` (`jfkit delete`) and
`jfkit.safedelete.evidence`.** Every rule below is a precondition, a category
or a measurement in one of those, and the dry run prints all of them.

Deleting media is not a technical problem. Getting it wrong is cheap to do,
impossible to undo and slow to notice: nobody looks for a film they were not
about to watch. So the shape of the tool matters more than any of its
individual checks.

---

## 1. Nothing is deletable by default

Not "nothing is deleted without `--apply`" -- that is true too, and it is not
the interesting part. **Nothing is deletable at all** until somebody names a
category of thing as released.

A candidate declares a category. A category is a *reason*, not a list:

| category | what it claims |
|---|---|
| `byte-identical-twin` | another file with the same bytes is kept |
| `media-free-folder` | the folder holds no media file, and no loose audio or subtitle track |
| `rebuild-donor` | this is the file a kept rebuild was made from |
| `superseded-copy` | a kept item, named in the manifest, covers this one |

The allowlist is passed in (`--release byte-identical-twin`), and a candidate
whose category is not on it is refused **with its category named**, rather
than skipped quietly. The difference matters: a tool that silently ignores
what it was not told about is a tool that reports success over a job it did
not do.

This is the shape a drawer of one-off deletion scripts had in common. What was
worth keeping was never any of their lists.

## 2. A manifest is a plan, and a plan is not evidence

Every precondition is checked against the world at the moment of the run, not
against what the manifest asserts:

- the item is still in the catalogue;
- the catalogue's path for it is the path the manifest claims;
- something is on disk at that path;
- nobody -- **any** user, not the one running the tool -- has a play count, a
  position, a "played" flag or a favourite on it. With no `--user`, every
  user the server lists (`GET /Users`) is checked; naming users narrows the
  check to them. A user list that cannot be read, or is empty, fails this
  check, so nothing is parked;
- a named twin or kept item is not the candidate itself -- not the same
  identifier, not the same file under another spelling. A file is always
  identical to itself;
- whatever the category requires is true right now;
- where a copy is removed because another is kept -- a twin, a superseded
  copy, a rebuild's donor -- **the kept copy is proved to play** (section 3).

A manifest written last week describes a library that has since been renamed,
rescanned and half rebuilt. A precondition that reads the manifest back to
itself is a comment.

## 3. Identity is measured, not assumed

`jfkit.safedelete.evidence.identical()` answers "is this the same file as
that one" in the order that costs least:

1. **sizes** -- a difference here is an answer, and it is free;
2. **digests** -- one sequential read of each, only if the sizes match.

A comparison that starts by hashing two files of different sizes is a slow way
to learn something that was available immediately.

### Prove the kept copy plays

Removing a copy because another is kept is only as safe as the kept one.
Its header proves nothing about it: a file whose body was reserved and never
written keeps a perfect header, and every comparison that reads headers --
container, tracks, languages, channels, duration -- prefers it, because it
claims the higher resolution. Keep it on that evidence and the copy that
plays is the one that goes.

So before anything moves, the kept file's **payload** is read
(`mkvkit.integrity`): a sampled zero-fill read, a scan of every packet
against the container's duration, and a full decode. A kept copy that fails
refuses the candidate; so does one that could not be checked at all -- no
evidence is not a pass. `--keeper-check quick` leaves the decode out; nothing
leaves the check out. The kept file is read once per run however many
candidates name it, and the result goes into the audit log.

## 4. A path that is not there has usually moved

`remap_path()` applies a list of rewrites and reports which one matched.

A candidate whose path does not exist is far more often a folder that was
renamed than a file that was removed, and "the file is missing, so remove the
row" turns a rename into a lost item. The remapper makes the guess visible:
the rule that matched is in the report, so somebody can disagree with it.

## 5. The folder is walked before anything moves

`folder_contents()` sorts everything beside a candidate into three piles:

- what **belongs** to it: sidecars sharing its name -- subtitles, artwork,
  a description file;
- **other media**: a different item that happens to live in the same folder;
- **everything else**: unaccounted for.

Another media file in the folder does not stop *this* file being removed, but
it absolutely stops the *folder* being removed, and that distinction is put in
front of a person rather than decided for them. A release folder that turns
out to hold a second film is the clearest possible reason to stop.

## 6. Nothing is deleted. Things are moved.

The file goes to a parking directory that keeps its layout. It stays there
until a person decides otherwise, and deciding that is not this tool's job.

The server is never asked to delete the item. On this server
`DELETE /Items/{id}` removes the item's **containing folder** from disk --
the file, its sidecars, its artwork, its extras and any other film that
shares the folder -- so a tool that parked one file and then called it would
destroy everything the parking was meant to protect. Instead the server is
told that the parked path was deleted (`POST /Library/Media/Updated`,
`UpdateType: Deleted`); its scan finds nothing at that path and drops the
row, and a scan deletes no files. A notification never names a library root.

"Freed" in the report means "moved out of the library", which is the number
somebody actually wants when they are deciding whether the work is worth
doing.

## 7. The order inside one item is the safety

    park the file  ->  confirm it arrived  ->  tell the server the path is gone

In that order, every time. A row removed first leaves a file that nothing in
the catalogue knows about, and those are found years later by accident, by
somebody who has no idea what they are. The report says whether the row had
gone by the time the run looked; where the scan has not got there yet, it
says so, and the row goes when the scan does.

Nothing is ever parked over something already parked. That earlier copy is
the one somebody may still need.

## 8. Every step is logged, including the ones that did nothing

The audit file is append-only, one line per check per candidate, with the
checks that **passed** in it as well as the ones that failed. It is the answer
to "what happened to this file", which is a question asked months later by
somebody who was not there, and a log of only the failures cannot answer it.

## 9. The dry run is the default and it is the useful one

It runs every precondition, reports every refusal with its reason, totals what
would be freed, and changes nothing. A run that passes its dry run and then
fails a precondition on the real pass has learned something real about the
library -- that is a feature, and it is why the preconditions are not cached
from the dry run.

---

## What a run looks like

```
jfkit delete manifest.tsv \
    --release byte-identical-twin --release media-free-folder \
    --parked /srv/parked --backup-folders /srv/parked/folders \
    --audit work/deletions.log
```

`manifest.tsv`, tab-separated, one candidate per row:

```
item_id	path	category	keeper	reason
00000000-0000-0000-0000-000000000003	/srv/media/movies/Blue Canyon (1998)/donor.mkv	byte-identical-twin	/srv/media/movies/Blue Canyon (1998)/keeper.mkv	same bytes, two folders
```

No `--user`: every user the server lists is checked. Pass `--user` (more than
once) only to narrow the check deliberately.

**Only catalogued things.** Every candidate is an item with its own row whose
catalogued path is the manifest's path. A file this tool, or `jfkit swap`,
already parked has no row of its own -- the row it had belongs to the file
that replaced it -- so a manifest naming it is refused on the path check,
every time. Removing a parked file for good is a person's decision, made
outside the toolkit.

## What is not implemented

- **No manifest builder.** Deciding what *should* be on the list is the
  expensive, judgement-heavy half, and it is not automated here. The
  evidence functions are the pieces somebody builds one with.
- **No undelete.** Moving a parked file back is a one-line operation and
  a deliberate decision; putting a verb on it would make it a routine one.
- **The categories are a starting set.** A caller may pass a category of its
  own; it then gets the general preconditions and no category-specific check,
  and the report says so by having nothing extra in it.
