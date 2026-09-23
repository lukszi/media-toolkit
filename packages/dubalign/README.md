# dubalign

Align a dubbed audio track to a different transfer of the same title: decode
once, measure the lag across the whole timeline, find the seams where it
jumps, splice on the measured seams, and verify the result against the
reference inside the rendered file.

```
pip install dubalign
pip install "dubalign[align]"   # changepoint detection and sinc resampling
```

Arrays are not an optional extra here -- they are what the library is. The
extra adds the changepoint detector and the resampling kernels.

Before any measurement is believed, the control harness runs the pipeline
against answers that are already known: a track against itself must read
zero, and a deliberately shifted pair must return the shift it was given.

See the repository root for the safety stance, the configuration model and the
contribution policy.
