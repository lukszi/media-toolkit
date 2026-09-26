# mkvkit

Matroska surgery and verification, and the shared foundation the rest of
`media-toolkit` is built on: the configuration model, external-program
discovery and the logging setup.

`mkvkit` is not on PyPI, and `pip install mkvkit` from PyPI is not available.
Install it from [the repository](https://github.com/lukszi/media-toolkit):

```
git clone https://github.com/lukszi/media-toolkit
cd media-toolkit
pip install -e packages/mkvkit
pip install -e "packages/mkvkit[langid]"    # spoken-language identification
```

or without a checkout:

```
pip install "mkvkit @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/mkvkit"
pip install "mkvkit[langid] @ git+https://github.com/lukszi/media-toolkit#subdirectory=packages/mkvkit"
```

The core is standard library only. ffmpeg, ffprobe and the MKVToolNix
command-line tools are discovered at run time; none of them is vendored, and
no install location is written into the code.

```
mkvkit probe    FILE...                     what is in it, by its headers
mkvkit integrity FILE...                    whether the payload is there and plays
mkvkit health   ROOT...                     every file of a library: sweep, then confirm
mkvkit chapters show|check|rollback|apply   the marks, and the document
mkvkit chapters classify|match|windows      somebody else's names: which job,
mkvkit chapters selfcheck|plan              whether they fit, what changes
mkvkit tags     show                        the tags, and what they overrule
mkvkit propedit FILE --track UID ...        change a header in place
mkvkit remux    FILE --staging DIR          rebuild it without some tracks
mkvkit verify   ORIGINAL BUILT              prove the difference was intended
mkvkit swap     KEEPER REPLACEMENT          park the old file, install the new
mkvkit copy     SOURCE DESTINATION          copy or move, proved on both sides
mkvkit sidecars PATH...                     what belongs to a video
mkvkit walk     ROOT                        a tree, without following links
mkvkit steps    show|status|apply PLAN      finish a saved plan from its audit
mkvkit langid   jobs|scan|report            what language is actually spoken
```

Everything that writes defaults to a dry run and needs `--apply`. A rebuild is
staged, verified against its own plan by `remux --apply` itself, and swapped
in; the file it replaces is parked, never deleted. An applied header edit
writes its rollback to disk before it touches the file. `docs/mkvkit.md` in
[the repository](https://github.com/lukszi/media-toolkit) is the guide.

One thing worth knowing before you use the chapter verbs: a published chapter
list whose timestamps match your cut proves the **marks** fit and says nothing
about whether the **names** were typed against them. `docs/methods/chapter-names.md`
is what to do about that. The bundled archive adapter is a scraper, ships
disabled, and is yours to enable deliberately or not at all. There is no
namer: this package checks names, it does not write them.

See the root of [the repository](https://github.com/lukszi/media-toolkit) for
the safety stance, the configuration model and the contribution policy.
