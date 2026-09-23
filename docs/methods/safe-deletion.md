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
| `media-free-folder` | the folder holds no media file at all |
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
  position, a "played" flag or a favourite on it;
- whatever the category requires is true right now.

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

"Freed" in the report means "moved out of the library", which is the number
somebody actually wants when they are deciding whether the work is worth
doing.

## 7. The order inside one item is the safety

    park the file  ->  confirm it arrived  ->  remove the catalogue row

In that order, every time. A row removed first leaves a file that nothing in
the catalogue knows about, and those are found years later by accident, by
somebody who has no idea what they are.

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
    --user 00000000-0000-0000-0000-000000000001 \
    --audit work/deletions.log
```

`manifest.tsv`, tab-separated, one candidate per row:

```
item_id	path	category	keeper	reason
00000000-0000-0000-0000-000000000003	/srv/media/movies/Blue Canyon (2004)/donor.mkv	byte-identical-twin	/srv/media/movies/Blue Canyon (2004)/keeper.mkv	same bytes, two folders
```

## What is not implemented

- **No manifest builder.** Deciding what *should* be on the list is the
  expensive, judgement-heavy half, and it is not automated here. The
  evidence functions are the pieces somebody builds one with.
- **No undelete.** Moving a parked file back is a one-line operation and
  a deliberate decision; putting a verb on it would make it a routine one.
- **The categories are a starting set.** A caller may pass a category of its
  own; it then gets the general preconditions and no category-specific check,
  and the report says so by having nothing extra in it.
