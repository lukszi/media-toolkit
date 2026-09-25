# Contributing

## Commit convention

Conventional commits: `feat(scope): ...`, `fix: ...`, `docs: ...`,
`test: ...`, `refactor: ...`, `perf: ...`, `build: ...`, `ci: ...`,
`chore: ...`. Subject under 72 characters, imperative mood, no trailing full
stop.

A body only when it adds information -- and when it does, the body is the
right place for the *method*: why this threshold, what it was measured
against, which failure it prevents. That is the part worth keeping.

No trailers. A commit message ends with its body: no attribution lines, no
identifiers, no tooling footers. The history carries one identity and nothing
else.

## The privacy rule

This code grew out of work on a private collection. Nothing about that
collection, the machine it runs on, or the people who use it appears here --
not in documentation, not in code, not in comments, not in test data, not in
file names, not in commit messages. It is enforced rather than remembered.

* Every example title, path, identifier and filename is invented, and comes
  from the fixed cast in `docs/CONVENTIONS.md`. There are no harmless
  exceptions: a real title in an example is an inventory entry about somebody
  else's collection.
* Nothing is ever "redacted in place". A redacted list is still a list: its
  length, its order and its years are still an inventory. Rewrite instead.
* No measurement from a real collection is published: no count, size,
  timing, rate or sample size taken from it. Documents carry the method, the
  code's own defaults and invented illustrations.
* Where a number was fitted rather than derived, say so and explain how to
  re-fit it.

## Reviewer checklist

The mechanical half runs in CI on every push and every pull request, and any
finding fails the run (mark the `ci` checks as required in the branch
protection rules and a failed run also blocks the merge); the rest is a person
reading the diff. Both halves apply to every changed file.

**Mechanical -- `.github/workflows/ci.yml`, blocking:**

- [ ] the deny scan is clean over the working tree, and over every commit of
      the change, one at a time -- its added lines (a root commit: its whole
      tree) and its message. The change is a pull request's commits since its
      base, or a push's commits since the one the branch pointed at before.
      A push with no earlier commit to compare against (the first push of a
      branch or tag, or a force push whose old tip is gone), the weekly
      scheduled run and a manual run scan every commit reachable from `HEAD`
      instead
- [ ] secret scanning (gitleaks) is clean over the commits a push or pull
      request brings, and over the full history on the weekly scheduled run
      and on a manual run (*Run workflow* on the Actions tab)
- [ ] no absolute path with a drive letter outside `examples/` and
      `docs/gotchas/windows-shell.md`
- [ ] no 32-hex identifier, and no globally unique identifier outside the
      all-zero fixture ones
- [ ] no host name or bare address other than the loopback address,
      `localhost` or `example.com`
- [ ] no e-mail address other than an `example.com` one
- [ ] no release-shaped filename token (a resolution next to a source tag, or
      a trailing group suffix) outside the invented cast
- [ ] `examples/mediatoolkit.example.toml` contains no value that looks like a
      secret

**Human, per changed file, blocking:**

- [ ] Could a reader learn any of: how many drives there are, which ones, how
      much storage, which libraries exist, how many people use the server,
      what is in the collection, where the server is reachable, or who those
      people are? If yes to any of them, rewrite.
- [ ] Is every title, path, identifier and filename in this file from the
      invented cast?
- [ ] Does a number quoted here only make sense for one particular collection?
      If so, remove it or generalise it.
- [ ] Does a comment speak in the first person about one particular setup,
      name a real person, or refer to a decision only one site made?
- [ ] Does a docstring describe one particular run rather than a capability?
- [ ] Do the tests pass with no media present and no network?
- [ ] If this file writes to somebody else's files: is the default a dry run,
      and does the apply path emit a rollback artefact first?

## Dry run and apply

Every command that modifies a file or a server record takes `--dry-run`
(the default) and `--apply`. Every apply that changes an existing file, record
or database writes its rollback artefact before it writes anything else, and
refuses to run when it cannot (`SECURITY.md` lists them, and the verbs whose
rollback is the parked original or that change nothing that exists). A change
that adds a writing path without both is not merged.

## Tests

```
git clone https://github.com/lukszi/media-toolkit
cd media-toolkit
pip install -e packages/mkvkit -e packages/jfkit -e packages/dubalign
pip install pytest ruff==0.15.0 mypy==1.19.1
pytest -m "not needs_ffmpeg and not needs_mkvtoolnix and not needs_asr"
```

The three packages go in with one `pip install` command: `jfkit` and
`dubalign` depend on `mkvkit`, and naming it in the same command is what makes
pip take it from the checkout. None of them is on PyPI.

That selection must pass on a machine with none of those programs installed.
With them installed, `pytest` runs everything except `needs_asr`, which wants
a speech model.

**No test touches a real library.** Every media fixture is generated from test
patterns and tones by

```
python -m tests.fixtures build
```

into `tests/_fixtures/`, which is not committed: the repository contains no
binary fixture at all, only the generator. Generation is deterministic and
takes seconds, and each fixture exists to make one known answer checkable --
a track whose tag contradicts its header, a container that is not what its
extension claims, a pair shifted by an amount the measurement must recover, a
chapter grid at times the code must reproduce.

Markers: `needs_ffmpeg`, `needs_mkvtoolnix`, `needs_asr`, `slow`,
`repository`. A test that needs an external program declares it; tests skip
themselves when the program is absent, so a partial toolchain reports skips
rather than errors. A test about the checkout itself -- the licences, the
changelogs, the example configuration, the deny scan over the tree -- is
marked `repository("path", ...)` with the files it reads, and skips, naming
them, when the suite is run somewhere those files are not (against an
installed distribution, say). In a checkout it always runs.

Style is `ruff check .` and types are `mypy` (strict), both with the
configuration in the root `pyproject.toml` and at the versions CI pins:
ruff 0.15.0, mypy 1.19.1.

## Adding a file at the root

`.gitignore` is an allowlist: its first rule, `/*`, ignores everything at the
top level, and only the directories and files named after it with `!` are
tracked. A new file at the root -- a `.gitleaks.toml`, a `CODE_OF_CONDUCT.md`
-- is therefore ignored silently: `git status` does not show it and
`git add` refuses it. Add a `!/NAME` line for it to `.gitignore` in the same
commit, and check with `git status` that it now appears. Files inside the
allowed directories are not affected.

## Splitting a package out later

The three packages live in one repository so that one chronological history
covers all of them. If one of them ever needs its own home:

```
git subtree split --prefix=packages/dubalign -b dubalign-only
```

carries its full history across, losslessly. Merging three repositories back
into one is not lossless, which is why this starts as one.
