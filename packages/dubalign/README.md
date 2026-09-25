# dubalign

Align an audio track from one transfer of a programme onto a different
transfer of the same programme, and prove the result.

You have a copy you keep, and another copy that carries a track you want — a
dub in another language, a better encode, a mix the keeper does not have. The
two are not the same master. The offset between them is not one number: it is
constant for a while, then jumps where one of them was cut differently, then
slides because the two were run at slightly different speeds. `dubalign`
measures all of that, works out where the joins go, builds the track on the
keeper's timeline, and then measures the result against the keeper from end to
end.

`dubalign` is not on PyPI, and `pip install dubalign` from PyPI is not
available. Install it from [the repository](https://github.com/lukszi/media-toolkit),
together with `mkvkit`, which it depends on, in one command:

```
git clone https://github.com/lukszi/media-toolkit
cd media-toolkit
pip install -e packages/mkvkit -e packages/dubalign
pip install -e packages/mkvkit -e "packages/dubalign[align]"    # a faster resampler and a more robust filter
```

or without a checkout:

```
pip install "mkvkit @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/mkvkit" \
            "dubalign[align] @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/dubalign"
```

Naming `mkvkit` in the same `pip install` command is what keeps pip from
looking for it on PyPI.

Arrays are not an optional extra here — they are what the library is. The extra
buys a polyphase resampler and an outlier-robust filter; everything has a path
that runs without it, and both paths are tested.

## The shape of a job

```
dubalign probe keeper.mkv donor.mkv

# the offset across the whole runtime, not at one point
dubalign map keeper.mkv donor.mkv --work ./work

# where it jumps, and by how much -- found, not typed in
dubalign changepoints keeper.mkv donor.mkv --work ./work

# the plan, as a document you can read and edit
dubalign plan keeper.mkv donor.mkv --work ./work --out plan.toml --apply

# build it, write it, and prove it
dubalign splice plan.toml donor.mkv --out built.f32le --apply
dubalign encode built.f32le --out track.flac --channels 6 --apply
dubalign verify keeper.mkv track.flac --work ./work
```

The decoded samples in `--work` are reused from one verb to the next, but only
for the same file: a dump's name carries a fingerprint of the source (its path,
track, size, modification time and the decode options), so a second programme
or a different `--other-stream` in the same directory gets dumps of its own. A
decode that is interrupted leaves no dump behind, and `verify` always decodes
afresh, because it exists to measure the file as it is now.

Reading is free. Every verb that writes takes `--dry-run`, which is the
default, or `--apply`, and there is no third state. The exit code carries the
answer: a verification outside the bar, a plan that fails its own check and a
control that did not reproduce its known answer all exit non-zero.

## What it is careful about

**A single measurement cannot tell a step from a slope from an offset.** So
the offset is measured edge to edge and reported as a curve, and the curve is
segmented into straight stretches with jumps between them.

**The joins go on the jumps.** Not at the next convenient pause. A join placed
a second away from its jump leaves the whole step uncorrected for that second —
which is not heard as a click, but as a passage that is out of sync.

**The span a join moves is what has to be quiet**, not the instant it sits at.
A join that closes a step of 200 ms repeats or drops 200 ms of material; inside
continuous speech the quietest *instant* is the gap between two consonants, and
a join placed there repeats a syllable at full level.

**Confidence is not correlation.** Every measurement reports the ratio of its
best match to its next-best. A high correlation with a second peak just behind
it is not an answer, and a periodic signal has one of those at every period.

**The plan is a document.** One segment per stretch, one seam per jump, in a
file you can diff, hand-edit and run again.

**Nothing is believed until the controls pass.** Four questions whose answers
were written down first, including one that exists only to catch a search
window so asymmetric that it cannot express the error it is looking for.

## Verification

`dubalign verify` scans the whole runtime at a fixed spacing and holds every
window to ±40 ms. Not the average — every window. Silent windows are skipped
and counted rather than passed, because silence correlates with silence at
every lag and counting that as a pass is how a scan reports a clean result for
a track that is not there.

Measure the finished file, not the samples you built. At least one common
encoder holds back 256 samples, which is 5.333 ms at 48 kHz: a perfect build
becomes a file that is late everywhere, and only a measurement of the file can
see it. `dubalign encode` trims that off by name.

## Try it on something you can generate

The fixture builder in [the repository](https://github.com/lukszi/media-toolkit)
writes `drift_pair.mka`: one container, two
tracks, the same invented programme in two transfers, with a gap at the head, a
stretch missing from the middle and a rate difference after it. The answers are
written down, the whole pipeline runs on it end to end, and the test that
proves the build is inside the bar is paired with one proving the *unbuilt*
transfer is not.

From the root of a checkout:

```
python -m tests.fixtures build
pytest -k end_to_end
```

## What it does not do

No video alignment, no cross-language spectral matching, no listening clips,
and no automatic plan that fills a stretch from a second source. The method
document lists each of those with what would be involved, rather than leaving
them to be discovered.

See `docs/methods/pal-alignment.md` for the method, what every number means,
and the mistakes each rule exists to prevent. See the root of
[the repository](https://github.com/lukszi/media-toolkit) for the safety
stance, the configuration model and the contribution policy.
