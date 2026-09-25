# Security

## Reporting

Please report a suspected vulnerability privately through the repository's
security advisory form rather than in a public issue. A first response is sent
within a few days.

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

Every change is scanned before it is merged, and the whole history of this
repository was produced under the same scan: the working tree, every commit
message, every patch and every reachable object. It looks for *shapes* rather
than for a list somebody remembered -- an absolute path, a drive letter, a
profile directory, a host name, an e-mail address, an identifier that could be
a real item id or key, a release-name-shaped token -- so a value nobody
thought to list still trips it.

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
* every apply writes a rollback artefact before its first write;
* every write is followed by a verification pass that compares the result to
  the original, at stream level, inside the final container;
* a container whose type is not what its extension claims is refused rather
  than edited, because the header editor would otherwise report success while
  changing nothing.
