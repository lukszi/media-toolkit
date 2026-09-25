# Changelog -- dubalign

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## Unreleased

### Fixed

- **A decode dump is only reused for the source it was made from.** Dumps were
  named by a fixed stem, the rate and the channel count, so a second programme,
  a different `--other-stream`, or the file being verified in the same `--work`
  directory was answered from an earlier file's samples. A dump's name now
  carries a fingerprint of the resolved path, the track, the file's size and
  modification time and every decode option. Dumps are written under a `.part`
  name and renamed into place only when the decode has finished, so an
  interrupted decode is never taken for a whole one. `dubalign verify` always
  decodes afresh. Dumps left by earlier versions are no longer found and can
  be deleted.
- **`dubalign encode --apply` no longer deletes its own input.** The scratch
  dump was named after the output, in the same directory, so built samples
  sharing a stem with the output were overwritten and then removed. The scratch
  dump now has a unique name of its own and is the only file removed.
- **A join is never placed past the end of the signal.** A span beyond the end
  scored as the quietest place of all. Only spans that lie wholly inside the
  signal are candidates, a jump past the end is clamped to it, and among
  equally quiet spans (digital silence) the one nearest the jump is chosen
  rather than the earliest.
- **The printed rate ratio is the one the plan uses.** `RateFit.rate_ratio` and
  the change-point segments' `rate_ratio` printed the reciprocal of the value
  written into a plan. Both are now `1 + slope`, the read ratio the splice uses,
  and `RateFit.speed_ratio` gives the reciprocal under its own name.
- **`epk_align.absolute_lag` no longer writes beside the source.** Its excerpt
  and dumps go into a private temporary directory (inside `work_dir` when one is
  given) that is removed before it returns.
- **The README's job ends by verifying a file the job creates.**

## 0.1.0 -- 2026-09-23

The first release. Align an audio track from one transfer of a programme onto
a different transfer of the same programme, and prove the result.

The measurement, the planning and the verification all run end to end on a
pair anybody can generate, and the test that proves a build is inside the
tolerance is paired with one proving the transfer it was built from is not.

### Added

- **`dubalign.align`** -- the primitives everything else is built from: the
  onset envelope, a normalised cross-correlation over every position of a
  signal, and the peak-to-second-peak ratio that is the confidence measure. The
  ratio, not the correlation, is what separates an alignment from one of forty
  equally good ones.
- **`dubalign.pal`** -- rate conversion written as the fraction the frame rates
  define (25025/24000, in lowest terms 1001/960) rather than a decimal, speed
  and pitch moved together through the sample rate and never by a tempo filter,
  a band-limited warp that applies an offset and a rate difference in one pass,
  and a pitch measurement that says whether a speed conversion was applied to
  the sound as well as to the picture.
- **`dubalign.decode` and `.probe`** -- one sequential read per source into two
  dumps, the full-rate signal and a reduced analysis copy, from a single
  invocation. Both of a stream's numbers are read and kept, the channel layout
  is read rather than inferred, and a track's true start time is carried rather
  than thrown away by the decode.
- **`dubalign.check_raw`** -- the check that stands between a decode and
  everything measured on it. A dump that lost its first packets correlates
  perfectly and is wrong by exactly what went missing.
- **`dubalign.densemap`** -- the offset across the whole timeline, edge to
  edge, as a curve rather than a number. A flat stretch is an offset, a
  vertical move is a cut, a slope is a rate difference, and only the curve
  tells them apart. Each series carries the confidence bar that belongs to how
  it was measured.
- **`dubalign.drift`** -- the fine measurement inside one stretch and the
  straight line through it, with the residual reported: a residual of tens of
  milliseconds is the series saying the stretch contains a step.
- **`dubalign.lag48k`** -- the offset resolved to whole samples at full rate,
  on the centre channel, with several points rather than one, because one point
  cannot tell a constant offset from a drift.
- **`dubalign.changepoints`** -- the piece the original work did not have. The
  jumps are detected, not read off a printout and typed in: exact optimal
  partitioning of a piecewise-*linear* curve, so a slope stays one stretch
  instead of becoming a staircase, with a penalty derived from the noise the
  series itself shows and reported alongside the answer.
- **`dubalign.boundary`** -- each jump pinned down to a few tens of
  milliseconds by running the correlation at the old offset and at the new one
  and finding where the answer changes. No threshold: the split that maximises
  the total evidence, over every possible split.
- **`dubalign.plan`** -- the splice plan as a document rather than as constants
  in a script, with a self-check that lists everything wrong with it before
  anything is built. Joins are placed *on* their jumps, in a bracket that is
  tighter the larger the step is, and scored on the span the join actually
  repeats or drops rather than on the instant it sits at.
- **`dubalign.splice`** -- equal-power crossfades of twenty milliseconds; a
  segment whose rate is exactly one is copied rather than resampled; gains are
  constants on named segments and nothing is normalised, compressed or limited.
- **`dubalign.channels` and `.loudness`** -- prove the two sources agree on
  channel order before mixing one into the other, and match level by integrated
  loudness over content both of them carry rather than by peak.
- **`dubalign.verify_pcm`** -- the whole runtime scanned at a fixed spacing,
  every window held to 40 ms, silent windows skipped *and counted*, and no
  summary statistic that can hide a point outside the bar.
- **`dubalign.controls`** -- four known-answer controls, including the one that
  exists to catch a search window so asymmetric that it cannot express the
  error it is looking for. They run on arrays with nothing installed, and again
  through the real programs and a real container.
- **`dubalign.epk_align`** -- the leading-gap correction, as code: an excerpt
  cut once with both streams and its timestamps kept, each stream's true first
  packet time read, and the index-space answer corrected by the difference.
- **`dubalign.encode`** -- the outputs, with the lossy encoder's own 256-sample
  delay trimmed by name rather than rediscovered by measuring a finished file.
- **`dubalign.cli`** -- ten verbs in the order a job uses them. Reading is
  free, writing is asked for twice with no third state, and the exit code
  carries the answer.

### Fixed while packaging

- A correlation over a window reports the *average* offset across it, so the
  measurement belongs at the **centre** of the window and not at its start.
  Labelling it at the start biases every point on a sliding stretch by half a
  window times the slope. It is a quarter of a millisecond until it is used to
  place a join, and then the join lands where the bias put it.
- The bar for an envelope measurement is not the bar for a waveform one.
  Envelope correlations between two independently encoded transfers live around
  0.1 to 0.3; holding them to a waveform's bar throws away every real
  measurement. Each series now carries the bar it was measured under.
- The control that runs through real programs was failing three of its four
  questions, and it was right to: its delayed track carries a *container*
  delay, which a raw decode discards, so the two dumps read as identical. The
  leading-gap correction is now applied there -- which makes that control a
  demonstration of the finding rather than a repeat of the array one.
- Estimating the measurement noise from a curve that had already been smoothed
  reads the smoothing as a measurement that went better than it did, and a
  detector penalty set from that splits one flat stretch into a dozen.

### Known limits

Stated here and in the method document rather than left to be discovered: no
video alignment, no cross-language spectral matching, no listening clips, and
a planner that emits a single source. Nothing here has been installed as a
distribution or built as a wheel, so the version is a claim about this tree and
not about an artefact.

## Unreleased -- 0.0.1

The package existed as a set of documented skeletons with one exception:
`dubalign.align` carried the alignment primitives. Nothing in it should have
been installed expecting it to do work, and the version said so.
