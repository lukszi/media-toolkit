# The swap procedure

**Implemented by `jfkit.swap` (`jfkit swap`) and `mkvkit.swap` (`mkvkit
swap`), with `jfkit.client.wait_idle`, `jfkit.refresh.safe_refresh` and
`jfkit.dto.compare` doing the parts around it.** Every rule below is a
function or an assertion in one of those. Where a rule is not implemented, it
says so.

Replacing a file that a media server already has in its catalogue is the most
dangerous minute in any of this work. It is the only step that touches a file
somebody would miss, and it is the only one where doing it *nearly* right
loses things that are not in the file at all: where somebody had got to, what
the item is called, which list it is in.

This is the procedure, in the order it has to happen, with the reason for each
step.

---

## 1. The path does not change, so the identity does not change

An item's identity is derived from its path. Not from its contents, not from a
stored identifier that follows the file around: from the path.

That single fact decides the whole design. Put a rebuild beside the original
under a new name and you have not replaced anything -- you have added a new
item with a new identifier, no play state, no place in anybody's list, and
left the old one pointing at a file that is about to disappear. Rename
afterwards and it is worse: a deletion, then an unrelated arrival.

Swap **in place**, under the exact path the original had, and every reference
the catalogue holds still resolves. Nothing has to be migrated because nothing
moved.

`mkvkit.swap.swap()` refuses a pair whose two filenames differ, for this
reason and no other.

## 2. Nobody is watching

    jfkit.client.wait_idle(timeout_s=..., poll_s=...)

One call. It blocks until no session is mid-playback, and gives up loudly
rather than quietly after a timeout.

Every destructive pipeline should begin with it, and in the scripts this
procedure was distilled from, not one of them did. Stopping a server under
somebody's playback is the failure that is remembered long after the batch job
is forgotten.

## 3. The service is stopped, per chunk, and the chunks are sized in bytes

The file being replaced is open. On some platforms that makes the replacement
impossible; on all of them it makes the result undefined.

So the service goes down. What matters then is how long for, and that is a
function of bytes, not of files:

    jfkit.swap.chunks(pairs, chunk_gib=..., mib_per_second=..., budget_s=...)

A chunk of short episodes and a chunk of long films can be the same file
count and two very different outages. A chunk therefore carries a byte total,
and where a copy rate is known, `Chunk.estimated_seconds()` turns it into a
predicted outage which is compared against a budget **before** anything stops.
Estimating afterwards is a report; estimating beforehand is a decision.

The service is restarted at the end of each chunk, whether the chunk worked or
not -- that is what `jfkit.service.stopped()` is for, and it is the failure
that matters: an exception halfway through must not leave the server down.

## 4. The original is moved, never deleted

    mkvkit.swap.parked_path(keeper, parked_dir)

It goes to a parking directory that mirrors its own layout, and it stays there
until a person decides otherwise. Disk is cheaper than a file you cannot get
back, and "the rebuild verified" is a claim about the checks that were run,
not about the ones nobody thought of.

The root of the original path is dropped on the way, because a root is a
property of one machine. Everything below it is kept, because a directory of
hundreds of files all called the same thing as each other is not a backup,
it is a puzzle.

## 5. The file that arrived is read again, in place

**Never a size comparison.** A rebuilt file legitimately has a different size;
that is generally why it was rebuilt. A size check here cannot fail for the
right reason and will pass for the wrong one.

What means something is reading the file at its destination: it opens, it
identifies as the container it claims to be, and it has the tracks the
replacement had. That is `mkvkit.swap.probe_check()`, and it is an argument,
because the right check for an audio-only rebuild is not the right check for a
full remux.

## 6. Any failure puts the original back

Before the error is reported, not after somebody has read it. The parked file
is moved into its old path again, so a failed swap leaves the tree exactly as
it was rather than in a state that needs a person to reason about at two in
the morning.

A batch stops at the first failure by default. The second failure usually has
the same cause as the first, and the cheapest moment to look at it is before
the rest of the batch has moved.

## 7. Then, and only then, tell the catalogue

    jfkit.refresh.safe_refresh(client, item_id, expected_changes=(...), until=...)

A refresh is a request, not an event: the call returns at once and the work is
queued. A record read a minute later can still be the record from before, and
a check that reads once and believes it will report the *old* stream table as
the new one -- on every item, quickly, and with no sign that anything is
wrong.

So the wait is a condition about the new file, not a sleep:
`jfkit.swap.expected_streams(n)` is the usual one -- the record shows the
number of streams the rebuild has.

## 8. Verification is a comparison of the record

    jfkit.dto.compare(before, after, expected=("MediaStreams", "Chapters"))

The swap was supposed to change the stream table and the marks. Everything
else -- the name, the overview, the provider identifiers, the air date, the
identifier itself -- was supposed to stay exactly as it was, and the
comparison reports anything that did not.

This catches the failure that is otherwise invisible for months: a library
configured to believe the container's embedded title rewrites the item's name
during the refresh that the swap itself triggered. The file is perfect. The
name is now the release name. Nothing errored.

## 9. Play state is snapshotted for every user and replayed

    jfkit.dto.user_data(client, item_id, users)
    jfkit.swap.replay_play_state(client, item_id, before)

It usually survives -- the row was never deleted, so nothing had to reattach.
"Usually" is not a thing to discover afterwards, the snapshot costs one call
per user, and a position belonging to somebody who was not consulted is not a
thing to lose.

Read it for **every** user, not for the one running the tool. An
administrator's own view of an item says nothing about the other people who
may be halfway through it.

---

## What a run looks like

```
jfkit swap plan.tsv --parked /srv/parked --chunk-gib 200 \
    --rate 180 --budget 900 --user 00000000-0000-0000-0000-000000000001
```

with `plan.tsv` holding one tab-separated row per item:

```
00000000-0000-0000-0000-000000000001	/srv/media/movies/The Quiet Harbour (1978)/keeper.mkv	/srv/staging/keeper.mkv
```

That is the dry run. It reports the chunking, the predicted outage per chunk
and where every original would be parked, and it changes nothing. `--apply`
is the same command with the writes turned on.

## What is not implemented

- **No parallel copying.** The chunks are worked through one file at a time,
  because the storage this was written against serves one sequential reader
  well and two badly. On storage where that is not true, this is leaving time
  on the table; see `docs/patterns/spindle-gate.md` for why the default is
  what it is.
- **No resume.** A chunk that fails halfway leaves its completed pairs
  swapped and its parked originals parked, which is a consistent state, but
  re-running the same plan re-checks everything from the beginning.
- **No check that the rebuild is the same content as the original.** That is
  the file side's job (`mkvkit verify`), it is a much larger measurement, and
  it belongs before the swap rather than inside it.
