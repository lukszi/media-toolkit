# Conventions

## The invented cast

Every example in every document, docstring, test, fixture, error string and
commit message in this repository uses a name from this list. There are no
exceptions, including harmless-looking ones: a real title in an example is an
inventory entry about somebody's collection.

Films: `The Quiet Harbour (1978)`, `Blue Canyon (1998)`, `Winter Tide (2011)`,
`Hollow Lantern (2004)`, `Golden Meridian (1987)`.

Series: `Northwind`, `Harbour Lights`, `The Longest Night`, `Signal Hill`.

Release-shaped example filenames, one of a series and one of a film:
`Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv` and
`The.Quiet.Harbour.1978.1080p.BluRay.x264-SAMPLE.mkv`. The second exists so
that a worked example can show what a container's own embedded title looks
like when a library is configured to believe it.

Example filenames used in docstrings and documents, and nowhere else:
`Northwind - S01E03 - The Quiet Harbour.mkv`,
`Harbour Lights 1-05 Pilot.avi`,
`Northwind.S01E03E04.mkv`, `Harbour.Lights.S01E02.German.DL.mkv`,
`Harbour.Lights.S01E02.mkv`.
The second one is the shape that a server reads as an episode *range*; it is
listed here so it can be written down in the module that predicts it.

Where a command-line example needs two files and a title would only get in
the way, the two sides of an alignment are `keeper.mkv` and `donor.mkv`, and
what comes out is `result.mkv`. They are roles rather than names, and they are
listed here for the same reason everything else is: they are the only media
filenames of that shape allowed in the repository.

Paths: `/srv/media/movies`, `/srv/media/series`, `/srv/staging`,
`/srv/parked`. A Windows example is written as a generic location, never as a
profile directory: `C:/Media/Movies` and `C:/Media/Series`.

Identifiers: `00000000-0000-0000-0000-00000000000N`.

## Numbers

Defaults, thresholds, tolerances and results on the generated fixtures are
reported exactly: they are properties of the code. Nothing is reported about
the material the tools were used on -- no counts of films, episodes, files or
marks, no sizes, no timings of a particular run. Where a constant was fitted
rather than derived, the document says so and explains how to re-fit it on
your own material.

## Fixture names

The generated media fixtures are part of the same cast and are listed here
for the same reason: the privacy gate reads this document, and these are the
only media filenames that may appear anywhere in the repository.

`tiny_multitrack.mkv`, `tiny_multitrack.mp4`, `not_really_mkv.mkv`,
`offset_pair.mka`, `drift_pair.mka`, `chapter_grid.mkv`,
`chapter_grid_pal.mkv`, `cueless.mkv`, `sample_cues.srt`, `tag_override.mkv`.

None of them is committed. They are built from test patterns and tones by
`python -m tests.fixtures build`, and each one exists to make a single known
answer checkable -- a language tag that contradicts a header, a container that
is not what its extension claims, a delay the measurement has to recover, a
grid of marks at times the code has to reproduce, a pair of transfers whose
offset steps once and then slides.
