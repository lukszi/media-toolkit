# jfkit

Jellyfin administration from the command line: the API client, whole-record
round-trips that do not empty the fields they omit, non-replacing refreshes
with drift detection, filename-parse prediction, library options with their
defaults filled in, database and preview upkeep, six read-only surveys,
in-place file swapping, evidence-first deletion, segment scoping, and a device
gate for the jobs that read everything.

```
pip install jfkit
```

Validated against Jellyfin 12.1. Not affiliated with the Jellyfin project.

`mkvkit` comes with it: the two packages share one configuration model, one
program-discovery path and one logging setup.

## Two things that are not optional

**Queries are user-scoped.** The unscoped collection route does not fail on
this server version -- it answers, with fewer items than exist, and says
nothing about the difference. An audit built on it under-reports silently.

**Writing is asked for twice.** Every command that changes anything takes
`--dry-run`, which is the default, and `--apply`. In the dry run every
mutating request is logged in full and not sent, so a whole pipeline can be
run against a real server and produce a complete account of what it would do.

## Start here

```
jfkit survey metadata --out work/surveys     # what is missing, and where
jfkit naming /srv/media/series --only-problems
jfkit jobs gate /srv/media/movies            # is the disk free?
```

All three read and change nothing.

`docs/jfkit.md` is the guide. The procedures with a method document of their
own are the swap (`docs/methods/swap-procedure.md`), deletion
(`docs/methods/safe-deletion.md`), the database
(`docs/runbooks/db-maintenance.md`) and the device gate
(`docs/patterns/spindle-gate.md`). Two fully invented worked examples are
under `examples/recipes/`.

Nothing here has been installed as a distribution: no wheel has been built and
nothing has been uploaded. The version is a claim about this tree.
