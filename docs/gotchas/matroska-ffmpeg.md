# Matroska and ffmpeg: things that cost a day

Every entry carries the **symptom**, the **cause** and the **fix**. An entry
that also carries a **Confirmed** line was reproduced deliberately, and that
line is the experiment; the shorter entries without one record a behaviour met
in the course of the work, which is weaker evidence and is why they are
shorter. These are empirical findings about particular versions of particular
programs, not documentation, and they go stale.

**Confirmed against MKVToolNix 84 and ffmpeg 7.1, September 2026**, over
files of mixed provenance. Where a number is quoted it is an illustration of
scale, not a constant of nature. A few later entries
were found while packaging this and were confirmed against MKVToolNix 97 and
ffmpeg 8.0; each of those says so where it is confirmed.

Every example uses an invented title and an invented path. `mkvkit` implements
the safe form of most of this; the module is named where it does.

Contents: [the muxer](#1-the-muxer) · [the header editor](#2-the-header-editor)
· [a tag can override a track header](#3-a-tag-can-override-a-track-header) ·
[proving two files are the same](#4-proving-two-files-are-the-same) ·
[measuring anything in time](#5-measuring-anything-in-time) ·
[chapters](#6-chapters) · [one reader per disk](#7-one-reader-per-disk)

---

## 1. The muxer

### 1.1 Passing chapters to a mux *adds* an edition

**Symptom.** Every source file that already had chapters comes out of the
build with exactly twice as many.

**Cause.** The chapters option is additive on an input. The source's own
chapters are only suppressed by the option that says so.

**Fix.** Pass the no-chapters option on the input alongside the chapters file,
every time. On a file that has already been built wrong, the header editor
replaces the whole chapters element in one header-only pass -- no remux.

**Confirmed.** A batch of outputs repaired in minutes; a sample of them was
then fully re-hashed per stream and every hash was identical to its value
before the repair, which is what makes the repair provably payload-safe.

**Guard worth copying.** If the replacement has *fewer* marks than the file
already has, restore the file's original chapters instead of applying it.
Applying it silently deletes the surplus positions. In one file in that batch the
replacement was several marks short of the file's own set.

### 1.2 A remux adds language subtags that were not there

**Symptom.** A strict header diff fails after a remux that changed nothing
meaningful: tracks that carried only the legacy language element have gained a
modern subtag element.

**Cause.** The muxer writes the modern subtags by default, on tracks and on
chapters.

**Fix.** Keep the source's own convention: pass the option that disables them
when no audio track in the source carries one. In verification, treat a
subtag appearing or disappearing as a **note**; compare the **legacy** element
strictly, because that is the one a probe and a media server read -- and fail
when both sides carry a modern subtag and the two disagree.

**Confirmed.** Across a whole rebuild pass this reduced a whole class of
spurious differences to zero while the strict legacy check stayed meaningful.

### 1.3 Track, edition and chapter identifiers are regenerated

**Symptom.** Every track's unique identifier differs between input and output,
and extracted chapter XML is not textually identical even when the marks and
names match.

**Cause.** The muxer mints fresh identifiers on every mux.

**Fix.** Treat it as a note, not a failure. Compare chapters as
`(count, start within 1 ms, name)` rather than as raw XML, and compare tracks
on type, codec, legacy language, name, channel count, sample rate, dimensions
and flags -- not on identifiers.

**Why it is safe.** Nothing outside the file references them, **and the muxer
remaps tags that target a track identifier along with them**. That matters
because such a tag can carry an overriding language (section 3): if the remap
ever failed, the per-stream language comparison in the same verification would
catch it. It never did.

### 1.4 The default output order groups tracks by type

**Symptom.** A file whose tracks were in a deliberate order comes out
reordered, silently.

**Cause.** With no explicit order, the muxer groups by track type.

**Fix.** Pass an explicit track order listing the original file's own tracks
in their original order, then the new ones.

**Confirmed.** Reproduced on a file whose audio had been placed after its
subtitles by an earlier pass.

### 1.5 A remux can change the video bytes without changing a single picture

**Symptom.** After a pure track-drop remux -- no video option passed at all --
the video stream's hash differs.

**Cause.** The muxer writes the codec's parameter sets in-band at every
keyframe, even when the source kept them only in the codec-private block. The
private block is identical on both sides; the keyframes are each larger by a
constant number of bytes.

**Diagnosis, which is the useful part.** A per-frame hash of the video on both
sides shows an identical frame count and identical timestamps and durations on
**every** frame; only the keyframes differ, and each by a constant amount --
typically a few dozen bytes, with one larger difference on the first
keyframe.

**Fix.** There is no fix that keeps the bytes identical. Decide what you
wanted: if the requirement was byte-identical video, exclude the file rather
than re-encoding it to make a hash match. A handful of files in one pass were
excluded on this basis.

**A companion finding.** The muxer strips a leading space from some text
subtitle lines -- a few bytes over a whole track -- while other text
tracks in the same files extracted byte-identical. The difference lives inside
the container's block, not in the text.

### 1.6 The muxer writes a seek index; the header editor cannot add one

A normal remux gives you a cue index for free. The header editor has no option
to add one: a file without cues can only be fixed by a remux.

This matters because a file with neither a seek head nor a cue list at the
start of the segment should be considered non-seekable -- so a **missing cue
index in a file you produced is a hard verification failure**, not a note. See
also 5.4, which is what a missing index costs at read time.

**Cheap detection.** Read the seek head at the front of the segment and follow
its entry pointing at the second seek head (a muxer typically writes a small
one at the front and the full one at the end). Only if no seek head names the
cue element do you walk every top-level element, which on a multi-gigabyte
file means thousands of random reads.

### 1.7 The identification output answers a narrower question than you think

It reports the **track header** language. It does not tell you that a tag is
overriding it (section 3). It reports no chapter times or names -- extract
those separately. And it parses other containers happily, so "it read the
file" is not proof that the file is Matroska (2.1).

Exit codes: zero is clean, one means warnings with a usable output, two or
more is a failure. Treat one as success and log the warning lines.

Fields that legitimately move on a **header-only** edit, and must be excluded
from an "only X changed" diff: the identification format version, the file
name, and the container's duration, muxing and writing application, and the
two date fields. Everything else must be equal.

### 1.8 The two programs spell a language code differently

**Symptom.** The same track comes back as `fre` from the identification output
and `fra` from the probe, and a comparison between the two reports a
disagreement on every affected track.

**Cause.** About twenty languages have two codes in the same standard, a
bibliographic one and a terminological one. The identification output prints
the bibliographic spelling; the probe prints the terminological one. Both are
correct and they are the same language.

**Fix.** Canonicalise every code the moment it enters your program and keep the
raw spelling beside it for reports. `mkvkit.langcodes.canonical` does that, and
`mkvkit.langcodes.bibliographic` converts back for the one element that insists
on the other spelling -- a chapter display's language.

**Confirmed.** MKVToolNix 97 and ffmpeg 8.0, September 2026, on a synthetic
file whose header was written as `fra` and read back as `fre`.

---

## 2. The header editor

### 2.1 It silently does nothing to a file that is not really Matroska

**Symptom.** A header edit succeeds -- exit code zero, no message -- and the
file keeps reporting the old value for ever.

**Cause.** The file is another container with a Matroska extension. The header
editor writes Matroska only; the identification tool parses the other
container happily, so a naive pipeline never notices.

**Fix.** Read the container type before writing and refuse anything that is
not Matroska, routing it to a remux instead. `mkvkit.propedit` does this and
will not be talked out of it.

**Confirmed.** Rare, but present in any large collection of mixed
provenance. The guard caught one mid-pass.

### 2.2 What it can and cannot change

**Can:** track header properties (language, the default, forced and enabled
flags, the track name); segment info fields, including deleting the container
title; the whole chapters element; the whole tags element; attachments.
Several edits combine into **one invocation**, which matters because each
invocation is one modification-time bump, and that bump is expensive
downstream (see the media-server reference).

**Cannot:** add a cue index (1.6). Touch a packet payload at all -- which is
load-bearing: a verification harness may **skip re-hashing the streams** after
a header-only edit, because the payload provably cannot have changed. Knowing
what cannot have changed, and skipping the measurement, is the difference
between a verification that runs and one that gets skipped.

### 2.3 Writing tags replaces *all* of them

**Symptom.** A new tag is written and an existing one disappears -- including,
worst case, the one that was overriding a track's language (section 3).

**Cause.** The tags option replaces the entire tags element.

**Fix.** Extract, parse, append your tag (and drop any previous copy of your
own tag, so re-runs are idempotent), write the merged document. Never write a
tags document you did not build from the file's current one.

### 2.4 Writing tags normalises the targets -- key your diff on the track identifier

**Symptom.** After a tags rewrite, a naive comparison reports every tag as
lost and re-added.

**Cause.** The editor re-emits the target block in its own normal form: it
drops the default target-type value and adds an explicit target type. It also
writes a tag-language element into every simple tag it touches, so even the
simple tags come back looking different from the ones you wrote.

**Fix.** Compare tags as a sorted list of `(track identifiers, name, value)`
triples and ignore the rest of the target block, the target type and the
per-tag language. `mkvkit.tags.TagSet.triples` is exactly that list.

**Confirmed.** With that keying, a pass that added one provenance tag to
a batch of files verified as "every pre-existing tag preserved, exactly one new
tag added", and nothing else.

### 2.5 A chapter-update failure can mean the *file* is broken

**Symptom.** The editor exits with a chapter-update failure, yet reading the
file back shows the chapters were written correctly.

**Cause, in the case observed.** The file was zero-filled from a byte offset
to the end -- most of it -- with an intact header, so the
segment's own seek target pointed into the zeros. The edit itself wrote into
header void space and damaged nothing; the zeros predated it.

**Why this shape is worth memorising.** It defeats the usual corruption
checks: the identification tool and the probe both report the **full**
duration, because the header is intact. Only a full sequential decode, or a
scan for a zero-filled tail, finds it.

### 2.6 Select a track by its identifier, not by its position

**Symptom.** A pass that edits "the second audio track" edits the wrong track
on the one file whose tracks are not in the order everything else is.

**Cause.** The editor's positional selector counts tracks of a type in file
order. That order is a property of how the file was muxed, not of what the
tracks are, and nothing keeps it consistent across a collection.

**Fix.** Every track carries a unique identifier; the editor will select on it
directly. Read the identifier in the same pass that decides what to change,
and the selector cannot drift from the decision. `mkvkit.propedit` takes an
identifier and refuses anything else.

**Confirmed.** MKVToolNix 97, September 2026. The positional form is still
what every hand-written pass reaches for, which is why this entry exists.

---

## 3. A tag can override a track header

**Symptom.** After setting a track's language with the header editor, the
identification tool shows the new value and the **probe** -- and therefore
anything built on the same library, including a media server -- still reports
the old one. A metadata refresh does not move it.

**Cause.** The file carries a tag targeted at the track's identifier with a
language name and value, and the demuxer lets that tag win over the track
header's own language element. The header editor's language option only
touches the header, so the two disagree and the tag is what the world sees.
Files muxed by the ffmpeg libraries commonly carry this element.

**Fix, header-only, no remux.** Extract the tags, back the document up, find
each tag whose targets name the track identifier, rewrite the language value
inside it (matching the name case-insensitively), and write the merged
document back. Then re-probe and assert the structure is otherwise unchanged.
`mkvkit.tags` does this, and `mkvkit.propedit` refuses a language write that
would leave the two disagreeing.

**Two rules that fall out of it.**

- **Verify a language edit with the probe, not only with the identification
  tool.** They answer different questions.
- **Lower-case the tag keys before comparing.** The probe returns the key in
  upper case when the value came from a tag and in lower case when it came
  from the track header. A case-sensitive comparison reports "no language" on
  every tag-sourced file.

**Confirmed.** A minority of the files in one tagging pass still read the old
value in the probe and in the media server after an edit the identification
tool reported as successful. After the tags fix, all of them read correctly.
The rest had no such tag and were unaffected.

**Second-order consequence.** Any later tags write on such a file must merge
rather than replace (2.3), or the override you just corrected is deleted --
or, worse, an old wrong one is resurrected.

**You can detect this from either program, and it is worth doing from both.**
The identification output carries the tag's value in a property of its own,
beside the header's -- so a single call tells you both what the header says and
what the file will actually be read as. The probe tells you the same thing a
different way: the key it returns is upper case when the value came from a tag
and lower case when it came from the header. `mkvkit.probe` keeps both and
reports the pair as a disagreement before anything writes, which is what lets
a language edit be refused rather than silently wasted.

**Confirmed.** MKVToolNix 97 and ffmpeg 8.0, September 2026, on a synthetic
file with an English header and a French tag: the identification output
reported both values in one call and the probe reported the tag's.

---

## 4. Proving two files are the same

### 4.1 Per-stream hashes are the primary evidence

Ask the decoder for one hash per stream with a stream copy -- no decoding, no
re-encoding -- on both sides, drop the deliberately removed streams from the
original's list by index, and compare the ordered lists. A mismatch is located
by position: "position k was original stream i".

Check the exit code on both sides too. A non-zero exit with a plausible-
looking list of hashes is a failure, not a pass.

This is a full read of both files, so it is the expensive step of any
verification. Read the two sides on **different physical disks in parallel**;
that is free throughput (each side reads at close to its full sequential
speed, simultaneously).

### 4.2 A per-frame hash separates "the pictures changed" from "the container did"

When a stream hash differs, a per-frame hash of that stream on both sides
tells you whether the coded pictures moved. Identical frame count, identical
timestamps and durations, and differences only at keyframes is the signature
of 1.5, not of a re-encode.

### 4.3 A seek-based probe lies; a full sequential decode is the truth

Seek-based probes report false corruption routinely. A full sequential decode
to a null output, with a zero exit code, is the oracle.

The converse is also real: a probe returning **both** its stream list and its
format block as null is genuine corruption. And 2.5 is invisible to both.

### 4.4 A shorter container duration can be correct

**Symptom.** After dropping an audio track, the output's container duration is
shorter than the original's -- by seconds, and sometimes by tens of
seconds.

**Cause.** The dropped track was the longest stream in the container and ran
past the end of the video.

**Rule.** A duration difference over one second is a failure **unless** every
kept stream's hash matched and the new duration is *shorter*; then it is a
note carrying the measured difference. If the hashes did not match, or the
duration grew, it stays a failure.

### 4.5 A rebuild renumbers the identifiers your tags are keyed on

**Symptom.** A rebuild that dropped one audio track verifies as having lost
every tag in the file, including tags on tracks that are still there.

**Cause.** Two things at once. Tags are keyed on the track identifier, and the
muxer regenerates every identifier when it writes a new file -- so the keys on
both sides are different numbers for the same tracks. And the muxer writes its
own accounting tags (byte counts, frame counts, duration, the writing
application), which it recomputes for the file it is producing.

**Fix.** Map the old identifiers onto the new ones by position among the kept
tracks before comparing, and exclude the accounting tags by name. What is left
is the tags somebody chose, which is the set worth defending. A tag about a
track that is genuinely gone went with the track: that is a note, not a loss.

**Confirmed.** MKVToolNix 97 and ffmpeg 8.0, September 2026. Found by running
a strict tag comparison across a real rebuild of a synthetic file, where it
reported nine lost tags and no real loss had occurred.

### 4.6 The discipline, in six lines

- Save a full identification signature **before** any edit and diff everything
  that must not have changed. "The command exited zero" is not verification,
  and the saved signature doubles as the rollback record.
- Know what provably cannot have changed, and skip measuring it (2.2).
- Run a known-answer control before trusting any measurement (5.5).
- Measure inside the **final** container, never the intermediate.
- Prove stream-level identity on **both** sides.
- A confidently wrong value is worse than a placeholder: prefer a no-op.

---

## 5. Measuring anything in time

### 5.1 A per-output option binds to the output it precedes

**Symptom.** One invocation with several mapped outputs takes minutes instead
of seconds, and produces one correctly-sized output and several enormous ones.

**Cause.** Options placed between the input and the first output bind to
**that output only**. A duration limit there leaves the rest unbounded, and
each of them decodes to the end of the file.

**Fix.** Repeat every output option in each output's own group. The working
shape for "one window, every track, one file open" maps each track to its own
output with its own duration limit and its own format options.

### 5.2 A raw dump ignores a stream's leading gap

**Symptom.** Cross-correlating two extracted audio tracks gives a biased
answer whenever the two streams have different container start times. In one
case a harness reported *identical* numbers before and after a real error of
several tens of milliseconds.

**Cause.** A raw dump starts at the stream's first packet. Content comes out
at `T + start_time(stream)` and the leading gap simply vanishes.

**Fix, either of two.** Cut both streams from the **same file in one
invocation** -- same window, two mapped outputs. Or work in absolute
timestamps: stream-copy an excerpt with timestamps preserved, read each
stream's true first-packet timestamp, and correct the index-space lag by the
difference:

    true_lag = index_lag + (first_pts[B] - first_pts[A])

Nothing is then assumed about seek behaviour or about gap handling. Validated
on a control pair -- a file's own compressed track against its own
uncompressed one -- to within a millisecond.

**Never** infer an offset from a stream's reported minimum timestamp.

### 5.3 A seek lands where it likes

Seeks land several seconds early on large files, routinely. Every
measurement needs a control that moves the seek by a known amount.

A related consequence when building audio against a decoded reference: a track
built sample-exact against another track's decoded samples must be muxed with
a synchronisation offset equal to that track's own start time, or it is early
by exactly that much -- often tens of milliseconds.

### 5.4 An unindexed container makes every seek a full read

Matroska without cues, other containers with the index at the end: every seek
parses from byte zero, so N sampling windows cost N full reads of the file.

**Fix.** Time the **first** seek. If it is slow, abandon the per-window path
and decode the whole track once, then slice the windows out of the decoded
audio in memory. Sixteen-kilohertz mono is about 115 MB per hour per track,
which is a cheap trade against reading a multi-gigabyte file several times.
`mkvkit.langid.worker` does exactly this and flags the result.

### 5.5 Known-answer controls, before trusting any measurement

Three of them, and all three are cheap:

1. a track measured against **itself** must read 0.000;
2. a synthetic pair built by muxing one track twice, the second with a known
   offset of +250 ms, must be recovered as +250.0;
3. the same pair measured at *t* and at *t+3 s* must give the same answer.

The third is what catches 5.3. A pipeline that has not passed all three is not
known to measure anything.

### 5.6 Asymmetric search windows silently clip the answer

If the reference window starts *w* earlier but the search range is only ±*w*,
the measurable lags are `[-w, +0.02w]`: positive errors read as approximately
zero and the result looks perfect. The symptom is several points reading the
same small positive number.

**Fix.** Use a search range of twice the window, and prove it with a control
that shifts the **data** by a known amount. Shifting the search window instead
cancels out and proves nothing.

### 5.7 Two things not to use as a reference

- **A lossless-compressed reference track** whose decoder discards frames
  until a major sync point: the head loss depends on how the stream was
  opened, and one pair gave three different answers. The frame-synchronous
  codecs do not have this problem.
- **A low-frequency effects channel, or a difference channel**, for
  cross-language work: correlation close to zero in practice. That content
  is not shared between a dub and its original.

### 5.8 Check the channel order before assuming it

Cross-correlate every channel of A against every channel of B and require a
clean diagonal -- high on the diagonal and clearly lower off it -- before assuming two multichannel tracks share an interleave order. This
also catches a declared layout mismatch that is not a remap.

### 5.9 A reported stream index is not a physical track order

For disc-structure sources, a catalogue's stream index is not reliably the
order of the audio tracks inside the resolved stream file: a consistent
off-by-one has been observed. Probe the stream file directly.

---

## 6. Chapters

### 6.1 Building the document

One edition entry, marked default. Per mark: a start time to nanosecond
precision, the hidden flag off, the enabled flag on, and a display block with
the name and its language **only when there is a real name**. Omitting the
display block entirely is the correct way to say "unnamed" -- a player labels
it generically. Never write a name like `Chapter 7` into a file; that is a
regression against the consumer's own normalisation, and it is not reversible
by looking at the file.

The chapter language element wants the **bibliographic** spelling of the
language code, not the terminological one. `mkvkit.langcodes` canonicalises in
the other direction and the chapter writer converts at the boundary.

Three source shapes, and what each may touch:

- the file has no chapters and the external set has names → external times
  (rate-scaled if needed) **and** names;
- the file already has chapters and the two grids agree → the **file's own**
  timestamps, external names mapped mark for mark, marks never moved;
- the file has no chapters and the external set has no usable names → times
  only.

### 6.2 Self-check every document before applying it

Mechanical, and all of these were earned:

- it parses; exactly one edition; the atom count equals the source set's;
- every atom has a start time; no atom is hidden;
- timestamps **strictly increasing**;
- first mark later than five seconds is a note, not a rejection;
- last mark at or past the runtime is a **rejection**; within thirty seconds
  of the end is a note;
- trailing marks at or past the end of the feature are **trimmed**, and the
  count is recorded -- disc chapter lists routinely carry one;
- reject names containing the replacement character: an upstream archive
  served broken encoding verbatim and the original byte is unrecoverable;
- reject "names" that are not names -- a set of raw timecodes, or
  `Chapter One` … `Chapter Nine` in any language. This was caught only after
  some had been written, because the "is it named?" counter accepted
  any non-empty string;
- reject a set whose names are verbatim identical to another title's,
  shared typo included: the archive entry is for the wrong title;
- reject a count mismatch against the file (applying it deletes positions --
  see 1.1), a file carrying **more than one** edition, and a file whose first
  edition has two marks on the same timestamp.

Apply every document to a scratch file and read it back before touching
anything real. Every document that passed the self-check also survived that, names and
encoding intact.

### 6.3 Rollback is stripping the names, not restoring the file

Where the marks were never moved, undoing a bad name set means re-emitting the
same start times with **no display block**. Generate the rollback document at
the same time as the change, and leave it unapplied: generating it is a
one-liner, applying it is a decision.

### 6.4 Apply, then verify twice

Verify against the **file**: re-read the identification signature and assert
that only the chapters element changed, after excluding the fields listed in
1.7.

Verify against the **consumer**: compare count, names verbatim, and start
positions. Tolerance used was one millisecond, and the worst differences measured
stayed inside it. A deliberately unnamed chapter matching the consumer's own generic
label is a **match**, and the comparison has to encode that.

The consumer's re-probe is queued, so a read a minute or more later can still
show pre-edit data. Poll until the count matches.

### 6.5 Rate-converted grids, in both directions

Where a transfer was rate-converted, a candidate chapter set may fit only
after scaling. Try the ratio, its inverse and 1.0; match the runtime the same
three ways. For each scale, take the per-mark differences, use their median as
the offset, and score the worst deviation from it: within two seconds is a
match, within five is loose, beyond that is a different cut. Equal counts and
at least three marks, or there is nothing to compare.

**The safety rule that matters.** A rate-scaled candidate may be used only
where the file's own timestamps are kept and only the names are copied -- the
grid agreement is itself the proof that the candidate describes this cut. It
may **never** be applied to a file that has no chapters, because there is no
grid to prove the edition matches. Empirically, few of the candidates that
matched only through the rate factor survived the two-second grid test.

### 6.6 A matching grid does not prove the names are right

This is the finding worth the whole section. A grid matching to within two
seconds proves the **marks** fit your cut. It says nothing about whether the
**names** were typed against those marks. On one worked case the grid matched
perfectly and many of the names described a scene a chapter or more away.

Verify names independently before writing them: a frame grab about three
seconds after each mark, plus a twenty-second transcript of the
original-language audio from the mark; a mechanical keyword hit-rate of the
name list against the transcripts at shifts −4 to +4, where a clearly better
score away from zero is the signature of a shifted list; and corroboration
from a second independent candidate whose grid also matches.

Both evidence artefacts come from **one seek per mark** -- the frame and the
audio window read from the same span -- so the disk sees seeks only and never
a sequential pass. On a real set, most lists came out aligned and a
meaningful minority came out uncertain or misaligned -- which is the argument
for checking at all.

The full write-up, with what to do about it, is
`docs/methods/chapter-names.md`; `mkvkit.chapters.verify` implements it.

### 6.7 A list of labels counted as a list of names

**Symptom.** Several titles came out of a naming pass carrying chapter names
reading `Chapter One` through `Chapter Nine`, the same label in another
language followed by a number, and in one case a list of raw timecodes. Each is *worse* than
having no names, because a player generates its own label for an unnamed mark
and now displays these instead.

**Cause.** The counter that decided whether a candidate "has names" tested
whether the string was non-empty. Every one of those entries is non-empty.

**Fix.** Count with a predicate that knows what a label looks like:
empty, a bare number, a bare upper-case roman numeral, a timecode, or the
word for a chapter in any language followed by a number -- spelled out as well
as in digits. `mkvkit.chapters.xml.is_generic_name()` is that predicate and
`chapters.sources.named_fraction()` is the count; a candidate under half real
names is a times-only list and is parked rather than written.

**Confirmed** by reading the written files back after the pass, MKVToolNix
97, September 2026. The word list grows whenever a published list turns up
spelling it another way, which is the honest state of that kind of check.

### 6.8 The entry may be a different film's list

**Symptom.** A long candidate list matched the marks, and none of the
names had anything to do with the film.

**Cause.** The list was character-for-character identical -- including a
spelling mistake -- to the list published for a different film in the same
series. Somebody had copied the wrong entry years earlier. The marks agreeing
was a coincidence of two films with similar act structure and the same running
time.

**Fix.** Compare a candidate against every other candidate you hold. A second
entry whose grid *also* matches and whose names are in the same order is
independent corroboration; an entry whose names are identical but whose marks
are **not** yours is proof the list belongs somewhere else.
`mkvkit.chapters.verify.corroborate()` reports both.

**Confirmed** by inspection of the two cached entries, September 2026. Among
titles that had a second matching entry, an identical name order was not
rare.

### 6.9 A flat character cap elides the middle of a long chapter

**Symptom.** A long chapter was named from its opening minutes and its
closing lines. The sequence the chapter is actually about was not
in the material the name was written from.

**Cause.** The window builder capped each chapter at a fixed number of
characters by keeping the head and the tail and eliding the middle. On a short
chapter that is invisible; on a long one it removes the chapter.

**Fix.** Size the budget by the chapter's own duration, and when text has to
go, drop it **evenly across the whole span** -- cut the chapter into slots,
take the same share from each, mark each elision. `chapters.windows.budget_for()`
and `build_windows()`. Everything over about ten minutes is at risk under a
flat cap.

**Confirmed** by re-reading the same chapter at a larger budget: the right name
was then obvious. September 2026.

### 6.10 A speech model's segments are far longer than a chapter boundary

**Symptom.** Dialogue from the start of one chapter kept turning up in the
evidence for the chapter before it, blunting every alignment score computed
from it.

**Cause.** A batched transcriber returns twenty or thirty seconds as a single
segment with one start time, and a segment straddling a mark was attributed
whole to the side it started on. Neither a voice-activity setting nor a chunk
length shortens those segments -- several attempts were spent finding that out.

**Fix.** Ask for word timestamps and re-cut the words into short cues
(`chapters.transcripts.cues_from_words()`), then split any remaining cue at the
mark it crosses, spreading its words across its own span in proportion to time
(`clip()` and `trim_to_marks()`). A cue that overlaps at all still contributes
at least one word, so a mark landing mid-sentence never silently empties a
window.

**Confirmed** against a batched speech model, September 2026; the re-cut ran
several times faster than real time on one consumer graphics card and
never touched the disk the media was on.

### 6.11 A shift score at the edge of its range is computed on almost nothing

**Symptom.** On a six-mark list, the offset −4 scored a perfect 1.0 and won,
because it had exactly two names left inside the list and both matched.

**Cause.** Sliding a list of *n* names by *k* leaves *n − |k|* pairs to score.
At the extremes of a ±4 range that is very few, and a small sample scores
extreme values easily.

**Fix.** Do not score an offset that cannot be scored on enough of the list:
at least three pairs, and at least half the names that can be scored anywhere.
`chapters.verify.shift_score()`'s `min_pairs` and `min_share`. A short list
simply has fewer usable offsets, which is the truth about it.

**Confirmed** while packaging, on a constructed six-mark case, September 2026.

---

## 7. One reader per disk

On large spinning disks, concurrency on one spindle does not add
throughput, it adds seeks, and the cost is catastrophic rather than
proportional. Measured:

- a short multi-window audio read per track slowed by more than an order of
  magnitude while a second reader held the same disk;
- two remux builds on one disk collapsed to a small fraction of their
  throughput when a third, seek-heavy reader started alongside them;
  cancelling it restored them within minutes;
- with three readers the machine's desktop froze on any access to that disk;
- a staging or output drive counts as a spindle too: a copy-out job and a
  build sharing one cut the build's throughput several-fold.

Two concurrent muxes on one disk is the sweet spot -- the bottleneck moves
from processor to spindle. Three is worse. (The muxer is single-threaded: one
process is bound by one core while the disk still has headroom.)

**A media server's own background jobs are the invisible third reader.** They
are launched on demand by anything that changes a file's modification time --
which includes every header-only edit you make. Look for preview-tile
generation, chapter-image extraction and segment analysis before scheduling
anything heavy, and remember that stopping them is a decision for whoever owns
the machine.

**The gate that works:** before each file, require that the server's analysis
task is idle, that the disk's busy percentage averaged over two minutes is
under twenty-five, and that no foreign decoder process has a path on that disk
on its command line. Tag your own children so the gate does not gate on
itself.

**And the bug worth copying the fix for.** Matching a bare drive letter in a
command line also matches a decoder's own protocol prefix ending in the same
two characters, so every job on one disk read as a job on another and a lane
sat blocked for nothing. Require a real path: a drive
letter followed by a separator, case-insensitively.

**What follows operationally.** Hash the two sides of a verification on
*different* spindles, in parallel. Read each file once: fold every pending
edit into a single write, and derive several pieces of evidence from a single
seek. Order work so a batch that invalidates preview images does not run
before the previous batch's regeneration has finished. And let long jobs run
detached and gate themselves, so they can wait indefinitely rather than being
scheduled optimistically.
