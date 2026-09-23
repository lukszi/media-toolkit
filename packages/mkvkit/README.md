# mkvkit

Matroska surgery and verification, and the shared foundation the rest of
`media-toolkit` is built on: the configuration model, external-program
discovery and the logging setup.

```
pip install mkvkit
pip install "mkvkit[langid]"    # spoken-language identification
pip install "mkvkit[align]"     # the numeric half, without a speech model
```

The core is standard library only. ffmpeg, ffprobe and the MKVToolNix
command-line tools are discovered at run time; none of them is vendored, and
no install location is written into the code.

```
mkvkit probe    FILE...                     what is in it
mkvkit chapters show|check|rollback|apply   the marks, and the document
mkvkit tags     show                        the tags, and what they overrule
mkvkit propedit FILE --track UID ...        change a header in place
mkvkit remux    FILE --staging DIR          rebuild it without some tracks
mkvkit verify   ORIGINAL BUILT              prove the difference was intended
mkvkit swap     KEEPER REPLACEMENT          park the old file, install the new
mkvkit langid   jobs|scan|report            what language is actually spoken
```

Everything that writes defaults to a dry run and needs `--apply`. A rebuild is
staged, verified and swapped in; the file it replaces is parked, never
deleted. `docs/mkvkit.md` in the repository is the guide.

See the repository root for the safety stance, the configuration model and the
contribution policy.
