# Deciding what language a track is in

**Implemented by `mkvkit.langid`: the evidence pass is
`mkvkit.langid.worker.scan_track()` (`mkvkit langid scan`), the decision is
`mkvkit.langid.ladder.aggregate()`, `trimmed_aggregate()`, `mixed_split()` and
`settle()` with the thresholds in `ladder.SettleBar`, the context rules are
`mkvkit.langid.priors`, the per-track escalation is
`mkvkit.langid.review.decide()` and the run-level output is
`mkvkit.langid.report` (`mkvkit langid report`).**

This document exists because two places in the code and one line in every
report this package writes say "the thresholds are defaults fitted against one
collection; see the method document". This is that document: what the
thresholds are, what they were fitted against, what the error rate was, and
how to re-fit them for a collection that is not the one they came from.

---

## 1. The shape of the problem

A detector run on twenty seconds of audio returns a probability vector over
languages. It is right most of the time and confidently wrong some of the
time, and the confidently wrong cases are not random: a dub recorded by
actors with a strong accent, a film with one scene in another language, a
track that is music with no speech at all, a title sequence in a language the
rest of the programme is not in.

So the detector is not the method. The method is what you do with several of
its answers, and the bar you make them clear before anything is written to a
file.

Three properties are worth stating before the mechanism, because they are
what the mechanism is for.

**Deciding is separated from listening.** Everything in `ladder.py` is a pure
function over stored probability vectors: no audio, no model, no file. The
expensive pass runs once and appends one record per window to a log; every
decision after that is arithmetic over that log. Change a threshold and you
re-decide a collection in seconds without touching a disk, and evidence
collected months ago is still usable. This is the single most useful design
decision in the module, and it is the one that makes the thresholds *safe* to
call defaults -- a default you can re-fit without another pass over the
library is a very different thing from one you cannot.

**Confirming is free, overturning is a claim.** Agreeing with a tag that is
already in the file writes nothing and costs nothing if it is wrong in the
sense that it leaves the file as it was. Overturning a tag rewrites metadata
and asserts that whoever set it was mistaken. The bars for the two are
therefore not the same, and making them the same is how a language pass
produces confident nonsense at scale.

**Refusing is a result.** The output of the ladder for a hard track is
`UNSETTLED` with a reason, and the run's report lists those tracks and what
would answer them. A queue of a few dozen tracks for a person to listen to is
a success; a file written with a wrong language tag is a defect that will
survive every future pass because it now looks like ground truth.

---

## 2. Aggregation

`aggregate()` sums log-probabilities across windows and reports the geometric
mean, with the margin over the runner-up and the fraction of windows that
agree with the winner.

The geometric mean is chosen on purpose. Under an arithmetic mean, eight
lukewarm windows outvote one window that is certain the track is something
else. Under a geometric mean, one strongly disagreeing window collapses the
score. That is the intended behaviour: a track two windows disagree about
should not settle, it should escalate to something that can tell you why they
disagree.

Windows with too little speech in them are not counted at all
(`window_min_speech_s`, 8 s of the 20 s window). A window of room tone
produces a probability vector like any other, and it is noise with a
confidence attached.

---

## 3. The bar

`SettleBar` holds every threshold in one frozen object so that none of them is
a number buried in an `if`. A caller of the library can set any of them; the
configuration file reaches two, `[langid].min_conf` and
`[langid].override_conf`, and the command line uses the defaults for the rest.
The defaults:

| | Standard | Overturning an existing tag |
|---|---|---|
| counted windows | 5 | 8 |
| geometric-mean confidence | 0.92 | 0.97 |
| margin over the runner-up | 0.50 | 0.50 |
| agreement fraction | 0.80 | 0.90 |

Two adjustments to that grid:

- **A short item** cannot yield five windows. Rather than refusing everything
  short, the bar takes what exists down to a floor of three windows, demands
  unanimity among them, and raises the confidence to 0.95.
- **No speech anywhere** in 60 s of examined audio makes the track a
  no-dialogue candidate rather than a low-confidence one, which is a different
  conversation and a different verdict.

Three context rules can lower the standard bar, and **none of them can settle
a track on its own**:

- **Tag-confirm.** An existing tag that any counted window agrees with, at
  half agreement or better, is confirmed. This is by far the most common
  outcome on a real collection and it writes nothing.
- **Prior-backed.** A release-name or sibling-track prior that agrees with
  the audio winner lowers the audio bar -- to 0.85 for a strong prior, 0.90
  for a medium one. It never applies against the audio, it never applies
  without audio, and `priors.blocking_reasons()` disables it entirely for
  material where context is systematically misleading.
- **Trimmed.** For a film with one scene in another language, `trim_frac`
  drops the 20 % of windows least favourable to the winner and re-aggregates
  -- but only with corroboration from two independent stages, and only where
  the untrimmed agreement was already at 0.75. Without those conditions,
  trimming is a licence to discard inconvenient evidence, which is exactly
  what it looks like.

`mixed_split()` handles the case the whole grid gets wrong: a track that
genuinely contains two languages. Three or more windows, at a quarter of the
total each, for two different languages is reported as mixed rather than
settled for whichever one happens to lead.

---

## 4. The escalation ladder

`review.decide()` runs the stages in order and stops at the first that
settles. Each stage is more expensive than the last and answers a different
question, which is the point -- five runs of the same detector on the same
audio are not five pieces of evidence.

1. **Dense sampling.** More windows, speech-gated, spread across the whole
   runtime rather than clustered at the front.
2. **A whole-track speech hunt.** Is there speech in this at all? Answers the
   no-dialogue case and separates it from "the detector is unsure".
3. **A transcription cross-check.** Script and function-word profile of what
   was actually said. Independent of the detector's own language head.
4. **A different model family.** Not another checkpoint of the same
   architecture -- a different family, so that its errors are not the first
   one's errors.
5. **A person, with a clip.** The ladder's job is to make this stage small,
   not to eliminate it.

Stages 3 and 4 have their own, lower bars (`text_bar`, `family_bar`) because
they are used as corroboration rather than as deciders, and **neither may
contradict an existing tag by itself.**

---

## 5. What the defaults were fitted against, and how

The numbers above came from one collection of mixed film and episodic
material. They are a fit, not a derivation. What follows is how each was set
-- the procedure, not the tallies, which describe that collection and nobody
else's.

**Calibration against existing tags.** The first pass ran the detector over
every audio track in the collection and compared its answer with the tag the
file already carried. Agreement was close to total overall, and higher
still in the top confidence band. That is what set the standard bar in the
first place: the bar sits where the disagreement rate stops falling.

**The overturn bar was set from the disagreements.** Of the confident
disagreements the first pass produced, most turned out to be an artefact of
how tracks were indexed inside disc-folder sources rather than a wrong tag,
and genuine mislabels were very rare. A rule that overturns tags on a
collection where almost every tag is right has to be much more
reluctant than one that labels untagged tracks, which is where the asymmetry
comes from rather than from taste.

**Rule A was checked against the wrong-tag cases.** In every case in that
collection where the tag was genuinely wrong, **no window voted for the tag**.
That is what makes a single agreeing window sufficient to confirm.

**Rule B was validated on degraded controls.** Tracks with a known answer were
degraded until the audio evidence was weak, and the prior-backed rule was run
against them: **no false settles.**

**Rule C was validated on both halves of the tagged collection**, where the
answer is known: **no false settles.**

**The ladder as a whole was validated on a held-out hard-case sample.** The
sample was the tracks the standard bar had already failed to settle -- the
adversarial cases, not a random sample. Nearly all of the labelled rows
settled, every one of them correctly -- **no false settles** -- and the few
left over went to a person. End to end, a queue of low-confidence tracks came
down to a handful that needed someone to listen, and those were genuinely
ambiguous: the kinds of case section 1 describes, such as accented dubbing or
a track with no dialogue at all.

**One tuning decision was deliberately not taken.** Films with a single
foreign-language scene failed the geometric-mean floor even where every
corroborating stage agreed. The rule that handles them (rule C) was designed
and gated *before* looking at how it scored on those films, because a
threshold moved after seeing which side of it the answer fell on is not a
threshold any more.

### What the numbers do not mean

They are a measurement of one collection's composition against one detector at
one size. A collection in one language only, or with a much larger share of
non-speech material, or scanned with a smaller detector, will have a different
error curve, and these thresholds will be either too loose or needlessly
strict for it. Nobody has measured how far they transfer.

---

## 6. Re-fitting them for your collection

The re-fit uses the same two facts the original fit used: most tags in a
library are right, and the tracks that disagree are where the information is.

1. **Scan.** `mkvkit langid scan` over the whole collection, writing evidence
   to a log. This is the only expensive step, and it is resumable.
2. **Treat your existing tags as approximate ground truth.** `mkvkit langid
   report` prints the overall agreement with the tags that were already there;
   it does not break it down by confidence. Split the per-track rows it writes
   (`--out-dir`) by their `confidence` column yourself. If agreement among the
   most confident tracks is not close to total, the detector or the extraction
   is the problem and no threshold will fix it.
3. **Put the standard bar where the curve flattens.** Sweep `min_conf` over
   the stored evidence -- no re-scan, this is arithmetic -- and read off where
   further confidence stops buying accuracy. That is `min_conf`, and
   `min_agree_frac` follows the same way (settable from the library only; see
   below).
4. **Set the overturn bar from your own disagreements.** Take every track
   where the audio contradicts the tag at the standard bar, and check them by
   hand. However rare a genuine mislabel turns out to be, the overturn bar has
   to be strict enough that your false-overturn rate is lower than your
   existing tag error rate -- otherwise the pass makes the collection worse.
5. **Build degraded controls for the context rules.** Take tracks with a known
   answer, shorten or degrade them until the audio bar fails, and run the
   prior-backed and trimmed rules against them. Any false settle here is
   disqualifying: those rules exist to lower a bar, and a rule that lowers a
   bar has to be measured at the bottom of it.
6. **Hold out the hard cases.** Validate on the tracks that failed the
   standard bar, not on a random sample. A random sample is 99 % of tracks
   nothing was ever going to get wrong.
7. **Write the numbers down with the sample they came from**, next to the
   configuration. A threshold without its fit is folklore within a month.

A re-fit of `min_conf` and `override_conf` is a configuration change and a
re-run of `mkvkit langid report` -- not a code change and not another pass
over the media. Every other threshold in `SettleBar` can be changed only by
constructing one in code and calling `review.decide_all(..., bar=...)` over
the stored evidence; there is no configuration key or flag for them.

---

## What is not implemented

- **No detector ships with this package.** `worker.Detector` is a protocol;
  the adapters are optional extras, and a second *family* for stage 4 is a
  separate extra again. Stage 4 is unavailable if you install neither.
- **Stage 5 is a person.** The package produces the queue: which tracks, and
  why. It does not cut a clip to listen to, it does not produce the answer,
  and there is no interface for recording one beyond writing the tag.
- **Nothing here re-fits the bar for you.** Section 6 is a procedure, not a
  command. `sweep`-style tooling over a stored evidence log would be a natural
  addition and does not exist.
- **The priors are only as good as the naming.** `priors.release_token_prior()`
  reads tokens out of a filename; a collection whose filenames carry no
  language token gets nothing from it, and gets no warning that it got
  nothing.
- **Mixed tracks are detected, not resolved.** `mixed_split()` reports that a
  track holds two languages. Deciding which one the container should claim is
  left to a person, and there is no per-segment output.
