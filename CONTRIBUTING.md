# Contributing

## Commit convention

Conventional commits: `feat(scope): ...`, `fix: ...`, `chore: ...`,
`docs: ...`, `test: ...`, `perf: ...`, `ci: ...`. Subject under 72
characters, imperative mood. A body only when it adds information -- and the
body is the right place for the *method*, which is the part worth keeping.

## The privacy policy of this repository

This code grew out of work on a private collection. Nothing about that
collection, the machine it runs on, or the people who use it appears here,
and that is enforced rather than remembered:

* every example title, path, id and filename is invented, and comes from the
  fixed cast in `docs/CONVENTIONS.md`;
* no absolute path, no drive letter, no profile directory, no host name and
  no e-mail address outside `examples/` and `docs/gotchas/windows-shell.md`;
* no identifier that could be a real item id, user id or API key;
* no measurement from a real collection is published -- documents carry the
  method, the code's own defaults and invented illustrations.

CI enforces the mechanical half of that on every pull request (see
`.github/workflows/ci.yml`): a deny scan over the working tree, the diff and
the commit message, plus secret scanning. A change that trips it is not
merged; nothing is "redacted in place" -- it is rewritten.

## Dry run and apply

Every command that modifies a file or a server record takes `--dry-run`
(default) and `--apply`. A command without a dry-run path is not merged.
Every apply writes a rollback artefact before it writes anything else.

## Tests

```
pytest -m "not needs_ffmpeg and not needs_mkvtoolnix and not needs_asr"
```

must pass on a machine with none of those installed. Media fixtures are
generated from test patterns and tones by `python -m tests.fixtures build`;
no binary fixture is committed, and no test touches a real library.

## Splitting a package out later

The three packages live in one repository so that one chronological history
covers all of them. If one of them ever needs its own home:

```
git subtree split --prefix=packages/dubalign -b dubalign-only
```

carries its full history across. Merging three repositories back into one is
not lossless, which is why this starts as one.
