# media-toolkit

Three small tools for keeping a self-hosted video library correct, in one
repository.

| Package | Install | What it does |
|---|---|---|
| `mkvkit` | `pip install mkvkit` | the file side: safe header edits, original-vs-rebuilt verification, chapters, Matroska tags, container probing, spoken-language identification |
| `jfkit` | `pip install jfkit` | the server side: API client, whole-record round-trip, non-replacing refresh, filename-parse prediction, library options, database and preview upkeep, surveys, evidence-first deletion |
| `dubalign` | `pip install dubalign` | aligning a foreign-language dub to a different transfer of the same title: decode, measure, find the seams, splice, verify |

`mkvkit` and `dubalign` are useful with no media server at all. `jfkit` talks
to Jellyfin 12.x over HTTP; everything here was validated against 12.1.

This project is not affiliated with, endorsed by, or part of the Jellyfin
project.

## Install

```
pip install mkvkit
pip install "mkvkit[langid]"      # spoken-language identification
pip install "dubalign[align]"     # changepoint detection and resampling
```

Python 3.11 or newer. The external programs (ffmpeg, ffprobe, and the
MKVToolNix command-line tools) are discovered at run time and never vendored;
speech-model weights are never vendored either -- the model name and its cache
directory are configuration.

From a checkout:

```
pip install -e packages/mkvkit -e packages/jfkit -e packages/dubalign
```

## Configuration

One TOML file replaces every hardcoded path, identifier and token. It is found
in this order:

```
--config PATH  ->  $MEDIATOOLKIT_CONFIG  ->  ./mediatoolkit.toml  ->  per-user
```

The per-user location is the platform's own: the roaming application-data
directory on Windows, `$XDG_CONFIG_HOME` (or `~/.config`) elsewhere, under
`media-toolkit/config.toml`.

```toml
[server]
url       = "http://127.0.0.1:8096"
token_env = "JFKIT_TOKEN"          # the token itself is never in this file
user_id   = "00000000-0000-0000-0000-000000000000"

[tools]
ffmpeg   = "ffmpeg"                 # a name on PATH, or an absolute path
mkvmerge = "mkvmerge"

[paths]
movies  = "/srv/media/movies"
staging = "/srv/staging"            # where rebuilds are assembled
parked  = "/srv/parked"             # where replaced originals go, and stay
work    = "./work"                  # logs, caches, evidence

[policy]
keep_languages = ["eng", "deu"]     # never dropped
default_audio  = "eng"
```

A full annotated file is in `examples/mediatoolkit.example.toml`. Every value
in it is invented.

Secrets are never written into it. The file names an environment variable
(`token_env`) or a command that prints the secret (`token_command`, e.g. a
password-manager invocation). A literal `token = "..."` is refused at load
time, with an explanation rather than a stack trace. The loaded configuration
object is passed explicitly to everything that needs it: no module-level
singleton, nothing that hands a caller a token by being imported.

Validation reports *every* problem in one message, not the first one it hits.

## Safety principles

These four are the reason the project exists in this shape.

**Dry run first.** Every command that changes a file or a server record
defaults to a dry run and requires an explicit `--apply`. A command with no
dry-run path is not merged.

**Park, never delete.** Nothing is removed. A replaced original is *moved* to
the configured parked directory and left there; the caller decides, later and
by hand, whether it goes. Deletion tooling produces evidence and a manifest --
it does not free space on its own.

**Verify both sides, inside the final container.** "The command exited 0" is
not verification. A rebuild is proved by collecting stream-level evidence from
the original and from the result -- track table, per-stream payload hashes,
chapters, container-level fields -- and comparing them against a declared
expected difference. Anything outside that difference is a failure. Cosmetic
differences are recorded with the reason they are cosmetic, never silently
allowed, and measurement happens in the rendered output, never in an
intermediate.

**Known-answer controls before trusting a measurement.** A measurement
pipeline is run against a pair whose answer is already known -- a track against
itself must read zero, a deliberately shifted pair must return the shift it was
given -- before any of its numbers are believed.

## About the numbers

Several constants here were fitted against one collection: language-
identification confidence bars, agreement fractions, window budgets. They ship
as documented defaults with the method used to fit them, so they can be
re-fitted on your own material. They are not universals, and nothing in the
documentation claims they are.

Nothing in the documentation describes the material they were fitted on: no
counts, sizes or timings of anybody's collection.

## Status

Published as-is while it is being built, under the PolyForm Noncommercial
License 1.0.0 (`LICENSE`): free to use, change and share for any noncommercial
purpose, which is not the same thing as an open-source licence.
`CONTRIBUTING.md` has the commit convention, the privacy rule every change is
checked against, and the recipe for splitting a package out into its own
repository.
