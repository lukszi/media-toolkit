# Resolving duplicates

**Implemented by `jfkit.dedupe` (`jfkit dedupe`).** Grouping is
`jfkit.dedupe.groups`, what a copy holds is `jfkit.dedupe.facts`, the rules
are `jfkit.dedupe.rules`, the reading is `jfkit.dedupe.resolver` and the plan
is `jfkit.dedupe.plan`. Each rule below names its function. The dry run
prints every verdict with its reasons, and every rule's threshold is a key in
`[policy]` or `[policy.dedupe]`.

Two copies of one film look like a problem with an obvious answer: keep the
better one. Almost every word of that is wrong somewhere. The copies may not
be of one film; "better" has at least five meanings that disagree with each
other; the better-looking copy may not play at all; and removing the other
one also removes whatever only it had, including somebody's place in it. A
duplicate cleanup done by hand gets each of these right most of the time,
which is not good enough for the one step in the job that cannot be undone
by the job.

So the resolution is one plan, built by rules the owner wrote down, and it
refuses whenever it cannot show its work.

---

## 1. What is a group

**Films share a provider identifier** (`find_groups`). Two rows with the same
TMDB, IMDb or TVDB identifier are candidates, and so is a third row linked to
them through another identifier. A group whose rows then disagree on an
identifier they both carry -- one TMDB number, two different IMDb ones -- is
`NOT_DUPLICATE`: one of them was identified wrongly, and that is fixed in the
metadata, not by parking a file.

**Episodes share a slot and an identifier.** The slot is the series'
identifier, the season number, the episode number and the number the
episode ends at, so a double episode `E01-E02` is not a copy of `E01`. The
slot is necessary and not sufficient: a folder that holds several shows
numbered in one sequence puts different episodes in one slot, and so does a
numbering clash. Every member must also carry a provider identifier they all
share. The catalogue's *name* is no evidence -- the server can give three
different parts of one special the same name -- and neither is the series
name: **two different shows can share a name**, and grouping by it produces
verdicts about two unrelated episodes. The series *identifier* is what
counts.

**Segments are not copies** (`segment_of`). A file whose episode number
carries a letter (`S01E01a`, `S01E01b`), a sub-number (`S02E00.1`) or a part
marker (`part1`, `Part II`, `cd2`) is one segment of something, and the
server may give every segment the same slot and the same identifiers. The
segment marker is part of the key: segments are never grouped with each
other, only with copies of the same segment, and a slot holding several
segments is reported as its own class, `segments`. `jfkit survey duplicates`
applies the same rule, and counts identifiers shared by segments separately.

**One file catalogued twice is one copy.** A group whose rows all name the
same file is `NOT_DUPLICATE`.

**An item with two media sources is two copies** (`members_of`): the server
merges two files in one folder into one row, and each is a member. They are
read and ranked like any other copies, so the verdict says which one the
rules would keep, but the plan does not park a version (`build_plan`): the
row's watched state and the file it plays are the server's to reconcile, and
the group is `BLOCKED` with that reason.

## 2. What each copy holds

Every member is read from its file, not from the catalogue (`probe_copy`):
one probe of its streams and format, and a listing of its folder for the
subtitle files beside it that belong to it (`mkvkit.sidecars`), which count
as subtitles. A track's language is canonicalised; a track with none is
`und`, which is a language of its own and matches only `und`.

Two things are the owner's to define:

| what | key | default |
|---|---|---|
| a lossless audio track | `lossless_codecs`: `codec` or `codec/profile` globs | TrueHD, MLP, FLAC, ALAC, PCM, DTS-HD MA |
| a commentary track | `commentary_markers`: words in the track title; the comment disposition always counts | `commentary` |
| a re-encode, and a source | `reencode_markers`, `source_markers`: words in the file or folder name; a re-encode word wins | `x264`, `x265`, rips; `remux`, `web-dl` |

A folder (a disc structure) is not probed: its group is `BLOCKED` and left
for a person.

**Every read goes through `mkvkit.lanes.map_by_device`**: one reader per
physical disk, the disks side by side. There is no switch for a second
reader. Before each read the device gate is asked (`jfkit jobs gate`) whether
anybody else is reading that disk or the server is running a task that reads
everything; the resolver waits up to `--gate-wait` seconds and then gives up
on that copy, which blocks its group.

## 3. May this copy replace that one?

`coverage(keeper, loser)` answers for one pair. The keeper may replace the
loser only if all of this holds:

1. **Every audio language of the loser, at the same number of channels or
   more**, commentary aside. A language in `policy.droppable_languages` may
   be lost; the loss is reported, not refused.
2. **Every lossless audio track**, by language: a lossless German track is
   not replaced by a lossy one, whatever else the keeper has.
3. **Every commentary track**, by language and count.
4. **Every subtitle language in `policy.keep_languages` that the loser has,
   and every forced subtitle in those languages.** With no `keep_languages`
   configured, every subtitle language the loser has must be kept.
5. **A running time no more than `runtime_tolerance_s` shorter** (default
   120 s). A copy whose running time cannot be read replaces nothing.

Two losses are allowed and are **reported** in the verdict's notes:

- subtitles in a language the policy does not keep;
- a second, lesser track in a language the keeper already has at the loser's
  best channel count -- a stereo downmix beside a 5.1 track, say.

## 4. Which copy is kept

Of the copies that may replace *every* other copy in the group
(`choose`), the keeper is the best under `policy.dedupe.prefer`, first
criterion first (`rank`):

1. `lossless` -- a lossless audio track;
2. `channels` -- the most channels on any audio track;
3. `source` -- a source over a re-encode, an unknown name in between;
4. `resolution` -- the resolution class (`resolution_class`): the larger of
   the width and the width a 16:9 picture of that height would have, rounded
   to the nearest common tier. **A scope film cropped to 1920x800 is the same
   class as the same film letterboxed to 1920x1080**; so is a 4:3 picture
   pillarboxed to 1440x1080. Counting pixels would prefer the black bars;
5. `bitrate` -- the last tie-breaker.

A tie on every criterion keeps the larger file and says so. The verdict
names the criterion each loser lost on.

**When no copy may replace all the others, both are kept** (`KEEP_BOTH`).
The classic case: one copy has the original language losslessly and English
only in stereo; the other has English in 5.1 and the original language lossy.
Each has something the owner's rules say must not be lost, and a tool that
forced a pick would be making a decision the owner has not made. The verdict
lists, for each copy, what it lacks. Copies whose running times differ by
more than `max_runtime_gap_s` (default 600 s) are different cuts, and are
kept too.

## 5. The keeper must play

This is the rule that matters most. A download that was
preallocated and never filled keeps a perfect header: the right container,
the right tracks, the right duration, a seek index. Every check above reads
headers, so every check above prefers it: a pass that trusts them keeps a
copy that is almost all zero bytes and removes the only one that plays.

So the chosen keeper's payload is read with `mkvkit.integrity.check()`
(`resolve`, through `default_checker`): a sampled zero-fill read, a packet
scan that adds up how much of the running time each track actually covers,
and -- with `keeper_check = "full"`, the default -- a decode of every track.
`quick` skips the decode and is still a read of the whole file. A keeper that
fails, or cannot be checked at all, makes its group **`BLOCKED`**: nothing in
it is parked, and the reason says that another copy may be the only one that
plays. The resolver does not fall back to the next-best copy on its own; that
is a person's decision, made with the report in hand.

The keeper is read again right before each copy is parked
(`apply_keeper_check`, default `quick`), and the park refuses when the
keeper's size has changed since the plan was made.

## 6. The plan

Every `SAFE` group becomes steps of one `mkvkit.steps` plan (`build_plan`),
in this order across all groups:

1. **Carry the watched state** (`userdata.write`). Every user's state on every
   copy is merged (`jfkit.userdata.merge`): played if any copy was, the
   highest play count, a favourite if any copy was, the latest last-played
   date, and the resume point of the copy played most recently. It is
   written onto the keeper where the keeper does not already hold it. Nothing
   is parked before this step. A copy whose state cannot be read blocks its
   group.
2. **Park each loser** (`dedupe.park`) through `jfkit.safedelete` with the
   category `resolved-duplicate`. The safe-delete preconditions run at that
   moment, against the world: the row and its path, the keeper's row and its
   file, **every user's state on the loser is held by the keeper**
   (`jfkit.userdata.carries`, instead of the usual "nobody has a position in
   it"), and the keeper's payload. The file is moved, never deleted, and the
   server is never asked to delete an item: on this server that call removes
   the item's whole folder from disk.
3. **Park the loser's own files** (`dedupe.park-sidecar`): its description,
   artwork, preview tiles and subtitle files, by `mkvkit.sidecars` rules.
   Folder-level artwork that belongs to no video stays with the folder.
4. **Park release folders left with no video** (`dedupe.park-folder`). Only
   the loser's own folder is a candidate; never a library folder or one that
   holds one, never a folder outside every library, never the keeper's. It
   is checked again when the step runs -- no media, no loose audio or
   subtitle track, nothing it could not look into -- and left in place,
   with the reason in the audit, if anything is there.
5. **Notify** (`dedupe.notify`): one notification naming the parked paths and
   nothing wider, so the server's own scan drops the rows whose files are
   gone. A folder directly under a library folder makes the server look at
   that whole library again; the plan says so beforehand.

Parking keeps the layout: `<volume>/a/b/c` goes to `<parked>/a/b/c`. A
relative parking folder is taken on each file's own volume, so a park is a
rename and never a copy between disks.

**The dry run is the default** and does all the reading: the catalogue,
every copy, every keeper's payload and every user's watched state. It prints
the verdict table and the plan, and `--json`, `--tsv` and `--plan-out` write
them. **`--apply` needs `--audit`**: the JSON-lines log every step is
recorded in. A run that stops -- a refused write, a file held open -- is
resumed by running the same plan with the same audit; finished steps are
skipped, and every step can tell whether its effect is already in place.

## 7. The verdicts

| verdict | meaning |
|---|---|
| `SAFE` | one copy may replace every other, and its payload was read and is there |
| `KEEP_BOTH` | no copy may replace all the others under the owner's rules, or they are different cuts; the reasons say what each lacks |
| `BLOCKED` | a copy could not be read, the keeper's payload failed or could not be checked, a copy's watched state could not be read, or the copies are versions of one row; nothing in the group is touched |
| `NOT_DUPLICATE` | the rows are not copies: distinct segments, one file catalogued twice, rows that disagree on an identifier, or episodes linked only by their slot |

The command exits 1 when any group is `BLOCKED` or an applied step failed.

## What is not implemented

- **No fallback keeper.** When the preferred keeper does not play, the group
  is blocked; the resolver does not pick the next copy by itself.
- **No partial resolution of a group of three or more.** When no single copy
  covers all the others, every copy is kept, even where one of them is
  covered by another.
- **No byte comparison.** Two byte-identical copies are resolved by the
  same rules as any other two; `jfkit delete` with `byte-identical-twin` is
  the tool for proving identity by hash.
- **No merge.** A copy that lacks one track the other has is not rebuilt
  with it; that is a remux, and a separate job.
- **No parking of one version of a row.** Copies the server merged into one
  row are ranked, never planned.
- **No series-level grouping.** A show split across two series entries is not
  found; merge the series first.
- **The name decides source or re-encode.** Nothing reads the stream for an
  encoder signature.
