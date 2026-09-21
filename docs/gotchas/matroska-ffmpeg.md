# Matroska and ffmpeg: things that cost a day

Every entry below carries the symptom, the cause, the fix, how it was
confirmed, and the exact version it was confirmed against. These are
empirical findings about one release, not documentation, and they go stale.

The first entries, in the order they were learned:

- a tag element carrying a language overrides the track header, and only one
  of the two is visible in a track table;
- passing chapters to the muxer adds an edition, so a file ends up with them
  twice unless the existing set is suppressed;
- the header editor silently does nothing to a file whose container is not
  really Matroska, and exits 0;
- track and chapter identifiers are regenerated on a remux, and that is fine;
- a per-output duration binds to the NEXT output only;
- a container with no index makes a per-window seek a full read.

Status: skeleton.
