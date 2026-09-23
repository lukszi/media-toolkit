# Aligning a dubbed track to a different transfer

Implemented by the `dubalign` package: `dubalign.densemap`, `dubalign.drift`,
`dubalign.changepoints`, `dubalign.plan`, `dubalign.splice`,
`dubalign.verify_pcm` and `dubalign.controls`, with the command-line verbs
`dubalign map | drift | changepoints | plan | splice | encode | verify |
controls`. Every rule below names the function that enforces it. A rule that
ships only as a paragraph is not shipped.

---

## The problem

You have two copies of one programme. One is the copy you keep — call it the
**reference**: it is the picture you will watch, and its timeline is the one
everything has to end up on. The other carries an audio track you want — a dub
in another language, a better encode, a mix the keeper does not have — and it
is a *different transfer*. Not a different encode of the same master: a
different master, made at a different time, from a different cut.

Four things can therefore be different, and usually at least two of them are:

| difference | what it looks like | what fixes it |
|---|---|---|
| a **constant offset** | one number, the same everywhere | copy at an offset |
| a **step** | the offset jumps at one instant and is constant either side | a join, at that instant |
| a **rate difference** | the offset slides, linearly, forever | resample |
| a **speed and pitch conversion** | the whole thing runs 4.27 % fast and a semitone sharp | resample, pitch following |

The last one has a name because it has a cause. A transfer made for 25-frame
broadcast runs faster than the 24-frame original by exactly **25025/24000**,
which cancels to **1001/960**. `dubalign.pal` holds that as a fraction and
never as a decimal: writing `25/23.976` costs 7.5 ms over two hours and writing
`1.0427` costs 60 ms, against a tolerance of 40 ms that everything else also
has to fit inside.

The first thing to understand is that **a single measurement cannot tell these
apart.** Measure the offset at one point and you get a number, with high
confidence, whichever of the four is happening. That number is right at the
point you measured it and possibly nowhere else.

---

## The method, in the order it runs

### 1. Decode once, sequentially — `dubalign.decode.decode`

One read per source, two outputs from it: the full-rate signal, which is what
gets spliced, and a reduced single-channel copy, which is what gets searched.

Sequential from the first packet, never a seek. A seek into a compressed
stream lands where the decoder can resume, which is not where it was asked, and
the difference is exactly the tens of milliseconds this whole exercise is
about.

`dubalign.check_raw.check_signal` then compares the dump against the duration
the container declared, because **a decode that lost its first packets measures
beautifully and is wrong by precisely the amount that went missing.** Every
window correlates, every confidence is high, and the correlation cannot notice
— a shifted signal is still a signal.

### 2. Map the offset across the whole timeline — `dubalign.densemap.dense_map`

Not at one point: edge to edge. A window of the reference is searched against
the **whole** of the other source, every few seconds, and what comes back is a
curve. A flat stretch is a constant offset. A vertical move is a cut. A slope
is a rate difference. All three are visible in the picture and none is visible
in a number.

The search runs on an **onset envelope** — short-term energy, logged,
differenced, half-wave rectified, at one kilohertz. What survives is where
sound *starts*. What does not survive is timbre, level and, usefully, language:
two different dubs of one picture share their onsets almost exactly and their
waveforms not at all.

The confidence is **not** the correlation. It is the ratio of the peak to the
next peak outside a guard band (`dubalign.align.best_with_ratio`). A
correlation of 0.95 with a second peak at 0.94 is not a measurement; one of
0.15 with nothing else above 0.06 usually is. That second number is what
distinguishes "this is the alignment" from "this is one of forty equally good
ones", and it is why a periodic signal — music, a repeating sting, a tone —
cannot fool the search into confidence.

Because the two bars are so different, `LagSeries` carries the bar it was
measured under. An envelope match between two separately-encoded transfers
lives around r = 0.1–0.3; a waveform match between two encodes of one master
lives above 0.9. Holding the first to the second's bar throws away every real
measurement.

### 3. Measure the stretches properly — `dubalign.drift.drift`, `.fit_rate`

The raw waveform, in a narrow bracket around what the map already found. The
point is the **slope**: inside a stretch that looks constant, the residual is
usually a straight line, and that line is the rate difference. One part in a
thousand is a millisecond a second — inaudible for ten seconds, past the
tolerance after forty, half a second by the end of a feature. It cannot be
fixed by moving anything.

`RateFit` reports its residual, and that is the half to read. A stretch that
really is one rate difference fits to well under a millisecond. **A residual of
tens of milliseconds means the stretch contains a step**, and fitting a line
across it produces a model that is wrong on both sides instead of right on
either.

One detail that is easy to get wrong and expensive to find: a correlation over
a window reports the *average* offset across it, so the measurement belongs at
the **centre** of the window, not at its start. On a stretch that is sliding,
labelling it at the start biases every point by half a window times the slope.
A quarter of a millisecond, until it is used to place a join — and then the
join lands where the bias put it.

### 4. Find the jumps — `dubalign.changepoints.find_changepoints`

This is the part that was missing from the work this package comes from. The
measurement said where the offset jumped; *finding* the jumps was done by
reading a printout, and the answers ended up as constants in a source file.
Constants found by eye cannot be checked, cannot be re-derived when the
measurement improves, and are quietly wrong.

The detector treats the curve as **piecewise linear** — deliberately linear and
not piecewise constant, because a stretch with a rate difference in it is a
slope, and a detector that can only fit flat lines chops every slope into a
staircase of imaginary jumps. The segmentation is exact rather than greedy:
optimal partitioning over every possible split, with a penalty per boundary, so
the answer does not depend on the order candidates were tried in.

The penalty is the one knob, and it is derived from the noise the series itself
shows rather than fixed once on one programme. It is reported in the output
either way.

A boundary whose step is too small to be worth a join is not reported *and not
kept*: the two stretches are merged and refitted, so the number of segments is
always one more than the number of jumps. A model that says otherwise cannot be
built.

### 5. Pin each jump down — `dubalign.boundary.crossing`

Detection resolves a jump only to the spacing of the measurements, which is
seconds. The refinement gets it to a few tens of milliseconds, and it is a
comparison rather than another search: read the two sources at the *old* offset
and at the *new* one along a short stretch, and ask which is right at each
moment. The old one works up to the cut and stops; the new one does the
reverse.

The estimator uses **no threshold**. It picks the split that maximises the
total evidence — the old offset's correlation before it plus the new one's
after it — over every possible split point. The obvious alternative (the first
moment the new one scores above some number and the old one below another)
depends on two constants and, on real material, fires early in a quiet passage
and late under music.

The two offsets have to be accurate, not approximate, and they have to *follow
the drift* where there is any: a fixed number is only right in the middle of
its stretch, and half a second either side of that it correlates at nothing.
`refine_changepoints` therefore re-measures the offset on material that is
unambiguously on each side before it locates the boundary.

### 6. Place the joins — `dubalign.plan.place_seam`

Three rules, all of them learned by getting them wrong.

**A join goes on the jump.** Not near it, not at the next convenient pause — on
it. A join placed a second away leaves the entire step uncorrected for that
second. For example, a join placed at a pause five seconds after a 100 ms
step leaves the whole of those five seconds 100 ms out. Nobody hears it as a
click, because it is not a click: it is a whole passage out of sync. The search bracket is
therefore narrow, and **narrower the bigger the step is** — one second above
40 ms, three seconds below. A large error must not be allowed to wait for a
nice pause; a small one can.

**Score the span that actually moves.** A join that closes a step of *S*
milliseconds repeats or drops *S* milliseconds of material. Scoring the
instantaneous level instead is what produced an audible doubled syllable:
inside continuous speech the quietest instant is the gap between two
consonants, so the join landed mid-word and repeated the word's own first
syllable at full level. What has to be quiet is the whole span that moves. The
seam records how far below its surroundings that span sits, in decibels, and
that number is the one that predicts whether the join will be heard.

**A step cannot be rate-ramped away cheaply.** Absorbing a step of *S* by
stretching the material around it costs at least *S*/2 of error somewhere,
spread out instead of concentrated. That variant was built and rejected on
measurement: at the joins it was tried on, it made the local error larger,
not smaller. A clean join at the right instant beats a smooth error
everywhere.

### 7. The plan is a document — `dubalign.plan.SplicePlan`

Not a list of constants in a script: a file, with one segment per straight
stretch and one seam per jump, that can be read, diffed, argued with, edited by
hand and run again. A join that landed badly is one number in a file. Two
builds of one programme differ by a diff somebody can look at.

Each segment carries the offset at its own start *and* the rate the source has
to be read at, which is what lets one pass over the material correct both. And
`SplicePlan.problems()` lists everything wrong with a plan before anything is
built: seams out of order, crossfades that overlap, a rate that is not a drift
correction but a different programme, a count of segments that cannot match the
count of joins.

### 8. Build it — `dubalign.splice.splice`

Each segment is read at the position the plan gives; each gain is applied; each
seam is crossfaded.

**Equal power, and short.** Sine and cosine rather than a straight line,
because two signals that are not identical add in power, not amplitude: a
linear fade leaves a 3 dB dip exactly where the listener is already listening
for one. Twenty milliseconds rather than two hundred, because a long fade is
audible as a swell even where the alignment either side is perfect.

(The converse is worth knowing: where the two sides *are* the same signal, an
equal-power crossfade adds up to 3 dB rather than staying flat. A join between
two segments of one source at one offset is not a free edit.)

**A segment whose rate is exactly one is copied, not resampled.** Most of a
spliced programme usually is a plain copy, and it should be provably so rather
than incidentally so.

**Nothing is normalised, compressed or limited.** Where one stretch comes from
a different transfer, the level is matched with a constant gain measured as
integrated loudness over content both sources carry
(`dubalign.loudness.match_gain_db`) — not as peak, because two masters
routinely differ by several decibels in loudness and by nothing in peak.
If their dynamic ranges genuinely differ, a constant gain is the honest fix and
the difference is worth writing down rather than processing away.

### 9. Prove it — `dubalign.verify_pcm.verify_against`

Four rules, each of which was learned by having got it wrong first.

**Measure the whole programme, not the joins.** The seams are the places
already thought about hardest. What goes wrong is elsewhere. The scan runs edge
to edge at a fixed spacing and reports every point.

**Every point has to pass, not the average.** A median of zero with a few windows
far outside the bar is a failure with a good average. There is no summary
statistic in the report that can hide the points outside the bar.

**Forty milliseconds, and the number is an argument.** Roughly one frame of
picture, which is about where a sync error stops being something a listener
feels and becomes something they see. A house bar, not a law of perception.

**Measure inside the finished thing.** Verifying the array that was built
rather than the file that was written from it checks the arithmetic and misses
the encoder. At least one common encoder holds back 256 samples — 5.333 ms at
48 kHz — which turns a perfect build into a file that is late everywhere.
`dubalign.encode` trims exactly that off the head, by name, out of leading
silence.

A window with nothing in it is skipped **and counted**. Silence correlates with
silence at every lag, and counting that as a pass is how a scan reports a clean
result for a track that is not there at all.

---

## What the numbers mean

**Lag, in milliseconds, signed.** Positive means the other source is late: the
reference's content at *t* sits at *t* + lag in the other source. Getting the
sign wrong is the most common mistake in this kind of code and it is invisible
on symmetric material, which is why one of the controls exists only to catch it.

**r, the correlation.** How similar the best match is. Read it against the bar
for the *kind* of measurement: an envelope match between two independent
transfers at 0.15 is good; a waveform match between two encodes of one master
at 0.15 is nothing.

**peak/second, the ratio.** How much better the best match is than the next
one. This is the number that says whether there *is* an answer. Below about
1.5, the measurement has found several equally good alignments and has no
opinion about which is right.

**Slope, in milliseconds per second.** The rate difference. Multiply by the
length of the stretch to see what it costs: 0.5 ms/s over 100 seconds is 50 ms,
which is outside the bar on its own.

**Residual, in milliseconds.** How badly a straight line fits a stretch. Small
means the stretch is one thing. Large means it is two things with a step
between them.

**Margin, in decibels.** How far below its surroundings the span a join moves
sits. Large is good. This is the number that predicts audibility, and it is not
the same question as whether the alignment is right.

---

## The controls — `dubalign.controls`

A measurement pipeline that has never been given a question whose answer is
already known is not evidence. It is a number generator that has not yet been
caught. Four controls run before anything else is believed:

1. **Zero.** A track against itself must read 0.000.
2. **A known offset.** A pair built with a delay somebody chose must give that
   delay back.
3. **The same answer twice.** The same pair measured at two different starting
   points must agree. This catches a decoder landing somewhere other than where
   it was asked — an error that is perfectly consistent within one measurement
   and different between two.
4. **A shift of the data, not of the window.** The subtle one, and the reason
   the first three are not enough. If the reference window starts *w* earlier
   but the search only reaches ±*w*, every measurable lag lies between −*w* and
   about zero: positive errors read as nothing and a badly broken pair comes
   back immaculate. The symptom is a run of points all reading the same small
   positive number. The only way to catch it is to move the **data** by a known
   amount and confirm the measurement follows. Moving the search window instead
   cancels out and proves nothing.

`array_controls()` runs all four on signals built in memory, with nothing
installed. `file_controls()` asks the same questions through the real programs
and a real container, because the arithmetic being right does not prove that
reading a container hands the arithmetic what it thinks.

The file version earns its place immediately. Its delayed track is built with a
**container** delay rather than with added silence, and a raw decode starts at
each stream's own first packet — so the delay is discarded on the way out and
the two dumps read as identical. Without the correction below, three of those
four controls pass and the pipeline is wrong about every real pair.

---

## The leading gap — `dubalign.epk_align.absolute_lag`

One finding deserves its own section, because it is the difference between a
measurement that is right and one that is confidently wrong by exactly the
amount being measured.

**A raw decode starts at the stream's first packet, not at time zero.** When a
track's first packet sits, say, 30 ms into a container, the raw samples that
come out start there: sample zero of the dump is 30 ms into the timeline. Correlating
two dumps whose streams begin at different times measures the difference
between them *plus* the difference in where they began, and nothing in the
correlation can separate those.

The fix does not require knowing how any decoder handles a gap. It requires not
depending on it: copy an excerpt with its timestamps preserved, read each
stream's true first-packet time, dump each to raw samples, and correct:

```
true_lag = index_lag + (first_packet[b] - first_packet[a])
```

Two relatives are in the same family. A seek lands where the decoder can
resume, so the excerpt is cut **once, with both streams in one command**, and
both halves land in the same place whatever that place is. And a track built to
be sample-exact against another track's *decoded* samples has to be muxed back
with that track's own start time as its delay, or it arrives early in the
finished file by exactly the head that was invisible.

---

## Demonstrating it end to end

`tests/fixtures/make_fixtures.py` builds `drift_pair.mka`: one container, two
tracks, the same invented programme in two transfers. The second carries a
120 ms gap at the head, a 40 ms stretch missing at 20 s, and a rate difference
of 48024/48000 after it — 0.5 ms/s, which is 50 ms by the end and therefore
outside the bar on its own.

The answers are written down in `tests/synthetic.py`, which is also where both
signals come from, so the file fixture and the in-memory pair the unit tests
measure are the same pair and there is one answer key to keep right.

The end-to-end test decodes both tracks, maps, detects, refines, plans, builds
and verifies — and then runs the scan a second time against the *unbuilt*
transfer, which must fail it. A verification that has only ever been run
against something correct is not known to work.

---

## What is not implemented

Stated here rather than implied by omission.

* **No video alignment.** A donor's audio can drift against its *own* picture
  by hundreds of milliseconds, and the right first step is to align the picture
  — after a rate conversion, frame *n* maps onto frame *n*, so the picture map
  is an integer frame offset — and treat the audio-minus-video difference as
  the defect. None of that is in this package.
* **No cross-language spectral alignment.** Where two tracks are genuinely
  different performances, a waveform correlation gives r 0.3–0.5 with ambiguous
  peaks, and a log-mel band correlation (40 bands, 5 ms hop, each band
  de-trended over a couple of seconds, each frame z-scored across bands) gives a
  clean margin on nearly every window. This package uses the onset envelope,
  which is language-independent but coarser. The band correlation is the
  obvious next measurement to add.
* **No listening material.** Clips around each join and level-matched A/B pairs
  are how a build is actually signed off, and the loop that produces them is
  not here.
* **No multi-source plans in the planner.** `SplicePlan` describes any number
  of named sources and `splice` builds them, but `plan_from_measurements` only
  ever emits one. Filling a stretch one source does not have — a logo, a promo,
  a recap — from the reference's own track is a plan somebody writes by hand
  today.
* **The low-frequency and surround channels are never used as references.** A
  dub's low-frequency and surround content was mixed separately from the
  original's, so correlating there compares two things that were never meant to
  match. `dubalign.channels` checks that the two sources agree on channel order
  before anything is mixed; it does not repair a disagreement.
