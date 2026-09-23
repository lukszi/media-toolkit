# Changelog -- dubalign

This file records what changed and why it is safe to install. Dates are the
day the work was done.

## Unreleased -- 0.0.1

Not released, and the version says so. The package is a set of skeletons with
one exception: `dubalign.align` carries the alignment primitives -- the onset
envelope, the normalised cross-correlation, and the peak-to-second-peak ratio
that is the confidence measure the rest of the method rests on.

Everything else -- decoding, dense mapping, drift, changepoint detection, the
splice plan, the encoder and the control harness -- exists as a documented
skeleton that raises rather than guesses. Nothing here should be installed
expecting it to do work yet.

The first release is planned once the measurement pipeline can demonstrate
itself end to end on a synthetic pair built from tones: a known offset
recovered exactly, a known drift corrected, and the seams found rather than
typed in.
