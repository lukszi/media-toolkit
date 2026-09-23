# Recipe: drop a dubbed track from one film, end to end

Everything here is invented: the film, the paths, the identifiers and the
numbers. It is a worked example of the shape of a job, not a record of one.

**The job.** `The Quiet Harbour (1978)` is a 31 GiB file with five audio
tracks. Four of them are dubbed tracks in languages this library has no use
for, and they are 9 GiB of the file. The job is to keep English and German,
drop the rest, and end up with the same item in the catalogue -- same identifier, same play state, same
poster, same name.

**What it costs.** Three commands that read, two that write, and about
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

```
| Item              | Stream | Claimed language | Codec | Channels | Default | Labelled | Estimated size (MiB) |
|-------------------|--------|------------------|-------|----------|---------|----------|----------------------|
| The Quiet Harbour | 1      | eng              | dts   | 6        | yes     | yes      | 2461.5               |
| The Quiet Harbour | 2      | deu              | ac3   | 6        | no      | yes      | 1230.8               |
| The Quiet Harbour | 3      | fra              | ac3   | 6        | no      | yes      | 1230.8               |
| The Quiet Harbour | 4      | ita              | ac3   | 2        | no      | yes      | 615.4                |
| The Quiet Harbour | 5      |                  | ac3   | 6        | no      | no       | 1230.8               |
```

Stream 5 claims nothing. **Do not guess.** A language code is a claim made by
whoever tagged the track, and an unlabelled track is a track nobody tagged --
which says nothing about what is on it. Identify it before deciding:

```
mkvkit langid scan "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --stream 5 --results work/langid.jsonl
mkvkit langid report --results work/langid.jsonl
```

```
stream 5: spa   confidence 0.981   margin 0.94   16/16 windows agree   CONFIRM
```

Spanish. It goes.

## 2. Build the replacement somewhere else

```
mkvkit remux "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    --staging /srv/staging --keep-language eng --keep-language deu --apply
```

```
staged /srv/staging/keeper.mkv
  kept 2 of 5 audio tracks, 1 video, 3 subtitles, 16 chapters
  22.1 GiB (was 31.2 GiB)
verify it with:
  mkvkit verify "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
      /srv/staging/keeper.mkv --dropped 3 --dropped 4 --dropped 5
```

The rebuild prints the verify command for what it just built, with the dropped
streams already filled in, because that is the step people skip.

## 3. Prove the difference is the one you asked for

```
mkvkit verify "/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv" \
    /srv/staging/keeper.mkv --dropped 3 --dropped 4 --dropped 5
```

```
16 kept stream(s): every hash matches
chapters: 16 marks, all within 1 ms, names unchanged
notes:
  the container duration shrank by 0.004 s: the dropped tracks ran past the
  rest, which is what removing them should do
  track identifiers were regenerated: nothing outside the file references them
PASS
```

Note what this is *not*: a size comparison. The rebuilt file is 9 GiB smaller,
which proves nothing at all. What proves it is that the streams that were kept
hash identically on both sides.

## 4. Swap it in

```
jfkit swap plan.tsv --parked /srv/parked --rate 180 --budget 300 \
    --user 00000000-0000-0000-0000-000000000001 \
    --user 00000000-0000-0000-0000-000000000002
```

with `plan.tsv`:

```
00000000-0000-0000-0000-000000000007	/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv	/srv/staging/keeper.mkv
```

The dry run first, which is what that command is:

```
1 chunk(s), 1 pair(s): dry run, nothing moved; 0 swapped
  chunk 1: 1 file(s), 22.1 GiB
  note: chunk 1: about 2 minute(s) of copying
  note: dry run: nothing was moved and nothing was stopped
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
  play state put back for 0 user(s)
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
remove it. When you are confident -- a week later, after somebody has actually
watched it -- that is a separate decision and a separate manifest:

```
jfkit delete parked.tsv --release rebuild-donor --parked /srv/parked \
    --audit work/deletions.log
```

which will refuse unless the kept item exists and its file is there.

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
