# A tour of everything

The front page gives the short version. This is the long one: every verb, and
the reasoning it carries, grouped by the job it does. Each package's
`CHANGELOG.md` says what arrived when, and each guide
([`mkvkit.md`](mkvkit.md), [`jfkit.md`](jfkit.md)) has the full reference.

**Edit a Matroska file, and prove you changed only what you meant to.**
`mkvkit probe` says what is in a file, from both programs that can describe
one; `mkvkit integrity` says whether its payload is actually there and plays,
which no header can, and `mkvkit health` asks it of a whole library, cheaply
first and one reader per disk. `mkvkit propedit` changes a track header in place, selecting the track by
the identifier it carries, and reads the file back to prove nothing else
moved. `mkvkit remux` rebuilds a file without the tracks a policy allows
dropping -- into a staging directory, never over the input -- and
`mkvkit verify` compares the two files against a *declared* difference, with
one payload hash per stream as the evidence. `mkvkit swap` then parks the
original and puts the rebuild in its exact path, once the rebuild is proved to
play, and `mkvkit copy` moves a file between volumes with both sides hashed.
`mkvkit chapters` and
`mkvkit tags` read, check and write the two elements that are easiest to
damage by accident. Everything that writes defaults to a dry run.

**And a chapter list that matches your marks may still describe another
film's scenes.** A published list whose timestamps agree with your file's to
within two seconds is certainly for your cut; that is no evidence at all about
its *names*, and a list can match that well while half of its names describe
a scene a couple of chapters away. `mkvkit chapters classify|match|windows|selfcheck|plan` is the
method as code -- which of three jobs a candidate is, whether the names sit on
the marks they describe, and a refusal rather than a guess where they do not.
`docs/methods/chapter-names.md` is the write-up.

The reasoning behind all of it is `docs/methods/verification-discipline.md`,
and every rule in that document names the function that implements it.

Two more things work end to end, and both of them read rather than write:

**Identify the spoken language of the audio tracks in a library.**
`mkvkit langid jobs` lists the tracks, `scan` reads them and records what a
detector heard per window, and `report` decides and says what is left. The
deciding is pure arithmetic over stored probability vectors, so it can be
re-run against evidence collected months ago when a threshold changes -- and
it runs with nothing installed.

**Put an audio track from one transfer onto a different transfer of the same
programme.** `dubalign map` measures the offset across the whole runtime rather
than at one point, because one point cannot tell a constant offset from a step
from a rate difference. `dubalign changepoints` finds the jumps instead of
having them typed in, and pins each one down to a few tens of milliseconds.
`dubalign plan` writes a document you can read, diff and edit, with the joins
placed on the jumps and scored on the span each one actually repeats or drops.
`dubalign splice` builds it, `dubalign encode` writes it, and `dubalign verify`
scans the whole result against the keeper and holds every window to 40 ms --
every window, not the average. `dubalign controls` runs four questions whose
answers were written down first, and nothing else is believed until it passes.

**Administer a media server without emptying the fields you did not
mention.** `jfkit survey` answers six questions about a collection --
completeness, audio languages per track, chapter state, containers,
duplicates, filename parsing -- in one shape, in five formats, with the scope
and the caveats inside the document. `jfkit item set` round-trips a whole
record and writes the phases in the one order that survives a refresh.
`jfkit refresh` asks for a non-replacing refresh, waits for the queue rather
than reading once and believing it, and reports everything that moved outside
what was expected. `jfkit swap` puts a rebuilt file in an item's exact path so
its identity, its play state and its name survive, with the service stopped
per chunk and the chunks sized in bytes. `jfkit delete` refuses to touch
anything whose category nobody released, checks every precondition against the
world rather than against the manifest, proves that a kept copy plays before
it gives up another, and parks rather than deletes. `jfkit leftovers sweep`
proposes the release junk, dead release folders, samples and unplayable files
a library collects, for the same checks and a resumable plan that parks, and
`jfkit leftovers missing` reports what the library lacks. `jfkit dedupe`
resolves copies of one film or episode into one keeper that covers every
other, carries everybody's watched state onto it and parks the rest, and
`jfkit rename` renames videos and folders with their sidecars and watched
state; both are one audited plan that resumes where it stopped.
`jfkit libopts`, `jfkit maintenance`, `jfkit segments` and `jfkit jobs` cover
library options, the catalogue database, scoping a segment pass, and not
putting two heavy readers on one disk.

**Predict how a media server will read a filename, before renaming.**
`jfkit naming PATH...` prints what each name would be read as and exits
non-zero if any of them would be read as an episode *range*, which is the
expensive mistake. It ports the server's naming, extras and season rules, is
pinned by a table of sixty-five invented names and by the upstream naming
tests translated into invented ones, and records the release it was checked
against.

Beside them, references that stand on their own: `docs/gotchas/` on one media
server release and on Matroska and ffmpeg, guides to each side in
`docs/mkvkit.md` and `docs/jfkit.md`, `docs/methods/` on the verification
discipline, the spoken-language settle ladder, aligning a dub to a different
transfer, the swap procedure, evidence-first deletion and resolving
duplicates, `docs/patterns/` on detached jobs and the device gate,
`docs/runbooks/` on looking after the catalogue database and on renaming
videos with their sidecars and watched state, and two fully
invented worked recipes under `examples/recipes/`. Every gotcha entry carries the symptom,
the cause and the fix; each reference states the versions its entries were
confirmed against, and an entry that names how it was confirmed is one that
was reproduced rather than met in passing; every method document names the function that implements each of its
rules and ends with what is *not* implemented, or says in its first paragraph
that it implements none.

**And one document about running the work rather than about using a tool.**
`docs/operating-playbook.md` is how a long cleanup is run safely when
most of the mechanical labour is handed to automation: one document that *is*
the state, and the discipline of refreshing it; a numbered ledger of
everything that surprised you, each entry pinned to a version; releasing a
whole category before anything in it may be touched; the dry run as a plan you
review and diff; staging elsewhere and swapping in place so identifiers
survive; verifying both sides inside the final container; one heavy reader per
device; detached, resumable jobs; one kept log per pass; what a person decides
and what automation decides; and how to hand the job to a reader with no
context. It is written for operators rather than for developers, and none of
it needs this repository installed.

Beside it, `docs/methods/handoff-audit.md`: how to find out whether that state
document is still true. Independent readers are pointed at the running system
and told to *refute* its claims rather than to review them, contested claims
are arbitrated and judged on a rubric of three verdicts and three severities,
and every one of them leaves with a corrected sentence. It is the only method
document here with no implementing function, and it says so in its first
paragraph.

**Nothing in the tree is a skeleton any more.** Every module that was one at
the first version of either package is implemented, tested and documented at
`jfkit` 0.3.0 and `mkvkit` 0.3.0. Each package's `CHANGELOG.md` says what
arrived when, and what each version deliberately does not do -- the largest of
which is that there is no chapter *namer* here, only the machinery that
checks names and refuses the ones that do not hold up.

