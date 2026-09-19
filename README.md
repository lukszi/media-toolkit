# media-toolkit

Three small tools for keeping a self-hosted video library correct, in one
repository:

| Package | What it does |
|---|---|
| `jfkit` | Jellyfin 12.x administration: API client, DTO round-trip, safe refresh, filename-parse prediction, library options, maintenance, surveys, evidence-first deletion |
| `mkvkit` | Matroska surgery and verification: safe propedit, original-vs-rebuilt comparison, chapters, tags, EBML probing, spoken-language identification |
| `dubalign` | Aligning a foreign dub to a different transfer: decode, measure, find the seams, splice, verify |

`mkvkit` and `dubalign` are useful without a media server at all.

## What this is not

It is not affiliated with, endorsed by, or part of the Jellyfin project.

It is not a downloader, a scraper of your neighbours' libraries, or a
"fix everything" button. Every tool that writes to your files defaults to a
dry run and emits a rollback artefact.

## Install

```
pip install mkvkit          # the file side
pip install jfkit           # the server side
pip install dubalign        # the audio-alignment library
```

Python 3.11 or newer. External programs (ffmpeg, MKVToolNix) are discovered
at run time and never vendored.

## Safety stance

* Dry run is the default for every writing command; `--apply` is explicit.
* Nothing is ever deleted. Replaced originals are moved to a parked directory.
* "The command exited 0" is not verification. Every write is proved by
  measuring the result, on both sides, inside the final container.
* Secrets are never written to a config file: the config names an environment
  variable or a command that produces the secret.
* Constants that were tuned against one collection ship as documented
  defaults with the method used to fit them, never as universals.

## Status

Published as-is, in the open, while it is being built. See `CONTRIBUTING.md`
for how the history was produced and how to send a change.
