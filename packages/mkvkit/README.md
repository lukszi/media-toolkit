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

See the repository root for the safety stance, the configuration model and the
contribution policy.
