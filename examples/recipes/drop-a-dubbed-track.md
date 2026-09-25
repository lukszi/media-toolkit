# Recipe: drop a dubbed track from one film, end to end

Everything here is invented: the film, the paths, the identifiers and the
numbers. It is a worked example of the shape of a job, not a record of one.

**The job.** `The Quiet Harbour (1978)` is a 23 GiB file with five audio
tracks: the original English, a German dub that stays, and three more dubs in
languages nobody watching it uses -- one of them not even labelled. The job is
to keep English and German, drop the other three, and end up with the same
item in the catalogue -- same identifier, same play state, same poster, same
name.

**What it costs.** A handful of commands, most of which only read, and about
fifteen minutes, of which the server is down for two.

---

## 0. Before anything: is the disk free?

```
jfkit jobs gate "/srv/media/movies/The Quiet Harbour (1978)"
```

```
/srv: held
  ffmpeg (48211) is already reading this device
  this device spins: one sequential reader at a time
```

That is the server regenerating preview tiles after last night's scan. Wait
for it. Two heavy readers on one spinning disk is slower than one, by a lot.

## 1. What is actually in there?

```
jfkit survey audio-languages --format md
```

The rows of that document (it also carries its scope, a summary and its
caveats):

```
| Item | Type | Stream | Claimed language | Codec | Channels | Default | Labelled | Estimated size (MiB) |
|---|---|---|---|---|---|---|---|---|
| The Quiet Harbour | Movie | 1 | eng | dts | 6 | yes | yes | 1165.7 |
| The Quiet Harbour | Movie | 2 | deu | ac3 | 6 | no | yes | 494.4 |
| The Quiet Harbour | Movie | 3 | fra | ac3 | 6 | no | yes | 494.4 |
| The Quiet Harbour | Movie | 4 | ita | ac3 | 2 | no | yes | 148.3 |
| The Quiet Harbour | Movie | 5 | (none) | ac3 | 6 | no | no | 494.4 |
```

Stream 5 claims nothing. **Do not guess.** A language code is a claim made by
whoever tagged the track, and an unlabelled track is a track nobody tagged --
which says nothing about what is on it. Identify it before deciding. The pass
is three commands: list the tracks, listen to them, decide. Only the middle
one needs the speech model (`mkvkit[langid]`); the other two run with nothing
installed.

```
mkvkit langid jobs "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --only-unknown --out work/langid-jobs.jsonl
mkvkit langid scan --jobs work/langid-jobs.jsonl --out work/langid.jsonl
mkvkit langid report --results work/langid.jsonl --out-dir work/langid
```

```
1 track(s) -> work/langid-jobs.jsonl
1 of 1 track(s) to read
1 track(s) -> work/langid.jsonl
1 track(s) -> work/langid
```

(The log lines each command writes beside that are left out here.) The
decision is in `work/langid/langid-results.tsv`, one row per track, and
`work/langid/langid-report.md` is the summary a person reads first:

```
path	stream_index	existing_tag	detected	verdict	rule	confidence	margin	agreement	counted_windows	reason
/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv	5		spa	settle	audio.standard	0.9810	0.9690	1.00	5	counted 5>=5, conf 0.981>=0.92, margin 0.969>=0.50, agree 1.00>=0.80
```

Spanish, settled on the standard bar by all five windows. The report proposes
the tag; writing it is a separate, deliberate step. `mkvkit probe` gives the
identifier the track carries:

```
mkvkit probe "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv"
```

```
  track  5 audio     AC-3                 und  und  uid 5555555555555555555
```

and a header edit writes the language in place, dry run first:

```
mkvkit propedit "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --track 1938578109198179986 --language spa
mkvkit propedit "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --track 1938578109198179986 --language spa --apply
```

```
keeper.mkv: dry run, 1 change(s)
  note: dry run: nothing was written
keeper.mkv: applied, 1 change(s)
```

Now it is a Spanish track, and it goes.

## 2. Build the replacement somewhere else

A rebuild drops nothing that a policy does not name, so the configuration
says which languages may go:

```toml
[policy]
keep_languages      = ["eng", "deu"]
droppable_languages = ["fra", "ita", "spa"]
default_audio       = "eng"
```

The original language is given on the command line, every time, because it is
the one language that must never be dropped and it is not something to guess:

```
mkvkit remux "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --staging /srv/staging --original-language eng --apply
```

```
keeper.mkv -> /srv/staging/keeper.mkv
  track 1 (eng): keep -- it is the original language (eng)
  track 2 (deu): keep -- deu is kept by policy
  track 3 (fra): drop -- fra is droppable and is not the original language
  track 4 (ita): drop -- ita is droppable and is not the original language
  track 5 (spa): drop -- spa is droppable and is not the original language
  built /srv/staging/keeper.mkv
  verifying:  mkvkit verify "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" /srv/staging/keeper.mkv --dropped 3,4,5
  PASS (tracks dropped): 16 stream(s) compared by hash
    note: 24 tag value(s) belonged to tracks that are not in the new file and went with them
```

The rebuild is not finished until it has been compared with its source, so
`remux --apply` runs that comparison itself: both files, one hash per stream,
against the plan's own declaration of what changed. A rebuild that does not
verify exits 1, stays in staging, and must not be swapped in. The command line
it prints is the stand-alone form of the same check, quoted and with the
dropped streams filled in.

## 3. Prove the difference is the one you asked for

The rebuild already did this once. Doing it again by hand, later, is how you
check a staged file nobody has touched since:

```
mkvkit verify "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    /srv/staging/keeper.mkv --dropped 3,4,5
```

```
PASS (tracks dropped): 16 stream(s) compared by hash
  note: 24 tag value(s) belonged to tracks that are not in the new file and went with them
```

The dropped streams are one comma-separated list. Giving `--dropped` once per
stream keeps only the last one, and the verification then fails -- correctly,
because it was told that a single stream went.

Note what this is *not*: a size comparison. The rebuilt file is about a
gigabyte smaller, which proves nothing at all. What proves it is that the
sixteen streams that were kept hash identically on both sides.

## 4. Swap it in

```
jfkit swap plan.tsv --parked /srv/parked --rate 180 --budget 300
```

With no `--user`, every user the server lists is snapshotted and put back; a
user list that cannot be read refuses the whole run before anything stops.

with `plan.tsv`:

```
00000000-0000-0000-0000-000000000007	/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv	/srv/staging/keeper.mkv	16
```

The fourth column is optional: the number of streams the record should show
once the server has read the new file. With it, the refresh after the swap is
polled until the record says sixteen; without it, only until the stream table
changes at all.

The dry run first, which is what that command is:

```
1 chunk(s), 1 pair(s): dry run, nothing moved; 0 swapped
  chunk 1: 1 file(s), 22.1 GiB
  note: chunk 1: about 2 minute(s) of copying
  note: dry run: every precondition was read; nothing was moved and nothing was stopped
```

Two minutes of outage for one film. Acceptable. Add `--apply`:

```
1 chunk(s), 1 pair(s): applied; 1 swapped
  chunk 1: 1 file(s), 22.1 GiB, service down 126s
```

What happened inside those 126 seconds: nobody was watching (checked), the
service stopped, the original was **moved** to
`/srv/parked/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv`, the
rebuild was copied into the original's exact path, the file at that path was
read back and found to have the tracks the rebuild had, and the service
started again.

Then, outside the outage: a non-replacing refresh, polled until the record
showed sixteen streams rather than nineteen, and a comparison of the record
before and after.

## 5. Read the comparison

```
00000000-0000-0000-0000-000000000007: swapped
  play state put back for 2 user(s)
  nothing else moved
```

That is the line that matters. The stream table changed, which is what was
asked for; the name, the overview, the provider identifiers, the air date and
the identifier itself did not.

Had the library been configured to believe the container's own title, this is
where it would have said so:

```
  Name: 'The Quiet Harbour' -> 'The.Quiet.Harbour.1978.1080p.BluRay.x264-SAMPLE.mkv'
```

-- the file perfect, nothing errored, and the item renamed to its own
filename by the refresh the swap itself triggered.

## 6. Afterwards

The original is still at `/srv/parked/...`. Nothing in this toolkit will
remove it, and `jfkit delete` is not the way to. That command only parks
things that have a catalogue row of their own at the path its manifest names.
The parked original has none -- the row it had now belongs to the rebuild at
the live path -- so a manifest naming it is refused on "the catalogue's path
is the manifest's path", every time, and naming the same item as its own
keeper is refused as well. When you are confident -- a week later, after
somebody has actually watched it -- deleting the parked file is a person's
decision, made by hand, outside the toolkit.

Where `jfkit delete` does belong is a donor that is still catalogued: say an
older copy of the same film that kept its own row in another folder. That is
a separate manifest, naming the donor's identifier and path and the kept
item's identifier:

```
item_id	path	category	keeper_id	reason
00000000-0000-0000-0000-000000000003	/srv/media/movies/The Quiet Harbour (1978) [old]/the-quiet-harbour.mkv	rebuild-donor	00000000-0000-0000-0000-000000000007	the rebuild is kept
```

```
jfkit delete donors.tsv --release rebuild-donor --parked /srv/parked \
    --audit work/deletions.log
```

That is the dry run; `--apply` does it. With no `--user` it checks every user
the server lists, and refuses if anybody has a position, a play count, a
"played" flag or a favourite on the donor, or if the user list cannot be
read. It refuses, too, unless the kept item exists, is not the donor itself,
and its file is there. What passes is moved to `/srv/parked`, and the server
is told that path is gone so that its own scan drops the row; nothing else in
the donor's folder is touched.

---

## The same job on a hundred films

Nothing above changes except the plan file. Build it from the survey:

```
jfkit survey audio-languages --out work/surveys
# work/surveys/audio-languages.tsv, one row per track, is the input to
# whatever you write to choose candidates
```

Then chunk the swap by bytes and give it a budget, so the outage is bounded
regardless of how big the files are:

```
jfkit swap plan.tsv --parked /srv/parked --chunk-gib 200 --rate 180 --budget 900
```

and read the per-chunk downtime in the report afterwards, because the estimate
was an estimate.
