# Security

## Reporting

Please report a suspected vulnerability privately through the repository's
security advisory form -- *Report a vulnerability* on the Security tab of
<https://github.com/lukszi/media-toolkit/security>
-- rather than in a public issue. A first response is sent within a few days.

If that form is not available (private vulnerability reporting is a setting
the repository has to switch on), open an
[issue](https://github.com/lukszi/media-toolkit/issues) that says only that you
have something to report privately and asks for a contact. Put no details of
the problem in it: not the component, not the input, not the effect.

## How secrets are handled

**A secret never has a value in a configuration file, and never appears on a
command line.** The configuration file names *where the secret comes from*:

```toml
[server]
token_env     = "JFKIT_TOKEN"                 # an environment variable
# token_command = "pass show media/server"    # or a command that prints it
```

Exactly one of the two may be set. The value is resolved when it is first
needed and passed explicitly to the client that uses it. No module-level
constant holds a token; importing a module never hands the importer a
credential.

A literal `token`, `api_key`, `secret` or `password` key with a value in the
configuration file is **refused at load time**, with a message that explains
the indirection instead of a stack trace. It is not read and then ignored: a
rejected file is a configuration error, because a value that reached a file
once will reach a backup, a paste and a screenshot.

`token_command` is run without a shell, with its argument list split by the
platform rules, and only its first line is used.

**Logging cannot leak it.** A redacting filter is installed unconditionally by
the logging setup. It holds the resolved secret values and replaces them with
`***` in any record it sees -- the message, its arguments, and the text of an
exception -- so a token cannot reach a log file through a request header in a
traceback. That filter is why `configure_logging` is called once per entry
point rather than left to each module.

## The gate

Every pull request is scanned before it is merged, and every push is scanned
when it lands (see `CONTRIBUTING.md` for exactly which commits each run
covers). The whole history of this repository was produced under the same
scan: the working tree, every commit message, every patch and every reachable
object. It looks for *shapes* rather than for a list somebody remembered -- an
absolute path, a drive letter, a profile directory, a bare address, a host name
or URL that is not loopback, a reserved example name or a public project host,
an e-mail address, an identifier that could be a real item id or key, a
release-name-shaped token -- so a value nobody thought to list still trips it.

Commit headers are not content, so they get a check of their own: every author
and every committer on every commit is the one published identity, and
`python tests/deny_scan.py --identities HEAD` fails on anything else. CI runs
it over the whole history on every event.

The gate has its own test, which plants findings and requires them to be
caught. A gate that has never failed is not known to work.

## This software edits your files

`mkvkit` and `dubalign` rewrite media files or produce replacements for them;
`jfkit` writes to a media server records through its API. Used carelessly, all
three can destroy data that has no second copy.

The mitigations are part of the design, not optional flags:

* a dry run is the default everywhere, and `--apply` is always explicit;
* no command deletes anything -- a replaced file is moved to the configured
  parked directory and left there. `jfkit delete` moves a released file there
  too and then notifies the server that its path is gone; it never calls the
  server's item delete, which removes the item's containing folder from disk;
* every apply that changes an existing file, record or database writes its
  rollback artefact before its first write, and does not run when it cannot:
  `mkvkit propedit`, `chapters apply` and `chapters plan` (`--rollback-dir`,
  default `<[paths].work>/rollback`), `jfkit item set` (`--backup`),
  `libopts set` and `segments scope` (`--backup-dir`) and `maintenance run`
  (`--snapshot-dir`). A rebuild never writes its source, a swap's rollback is
  the parked original, a preview restore only adds what is missing, and a
  refresh, a path notification or a scan cancel asks the server to do work and
  has nothing to roll back;
* what is verified after a write, exactly: `mkvkit remux --apply` hashes every
  stream of both files and compares them against the rebuild's own plan before
  it exits; a header edit (`propedit`, `chapters apply`, `chapters plan`)
  re-reads the file and compares it track by track -- header level, because a
  header editor cannot move a packet; `mkvkit swap` re-reads the arrived file
  for its track count and container type (run `mkvkit verify` against the
  original for the stream-level proof); `jfkit swap` waits for the server's
  record to settle and compares it with the one before; `jfkit delete` re-reads
  the catalogue after the file is parked;
* what is proved before a copy is given up, exactly: `jfkit delete` reads the
  payload of the copy that is kept (twins, superseded copies, rebuild donors),
  and `mkvkit swap` and `jfkit swap` read the replacement's, before anything
  moves -- a sampled zero-fill read, every packet against the container's
  duration and, unless `quick` is asked for, a full decode (`mkvkit
  integrity`). A copy that fails, or that cannot be checked, refuses the
  operation;
* a container whose type is not what its extension claims is refused rather
  than edited, because the header editor would otherwise report success while
  changing nothing.
