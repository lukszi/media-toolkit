# Conventions

## The invented cast

Every example in every document, docstring, test, fixture, error string and
commit message in this repository uses a name from this list. There are no
exceptions, including harmless-looking ones: a real title in an example is an
inventory entry about somebody's collection.

Films: `The Quiet Harbour (1978)`, `Blue Canyon (1998)`, `Winter Tide (2011)`,
`Hollow Lantern (2004)`, `Golden Meridian (1987)`.

Series: `Northwind`, `Harbour Lights`, `The Longest Night`, `Signal Hill`.

Release-shaped example filename:
`Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv`.

Paths: `/srv/media/movies`, `/srv/media/series`, `/srv/staging`,
`/srv/parked`. A Windows example is written as a generic location, never as a
profile directory.

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
`offset_pair.mka`, `chapter_grid.mkv`, `chapter_grid_pal.mkv`, `cueless.mkv`,
`sample_cues.srt`.

None of them is committed. They are built from test patterns and tones by
`python -m tests.fixtures build`, and each one exists to make a single known
answer checkable -- a language tag that contradicts a header, a container that
is not what its extension claims, a delay the measurement has to recover, a
grid of marks at times the code has to reproduce.
