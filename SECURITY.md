# Security

## Reporting

Please report a suspected vulnerability privately through the repository's
security advisory form rather than in a public issue. A first response is
sent within a few days.

## This software edits your files

`mkvkit` and `dubalign` rewrite media files in place or produce replacements
for them; `jfkit` writes to a media server's database through its API. Used
wrongly, all three can destroy data that has no other copy.

Mitigations that are part of the design, not optional flags:

* dry run is the default everywhere; `--apply` is always explicit;
* no command deletes anything -- replaced files are moved to a parked
  directory and left there;
* every write is preceded by a rollback artefact and followed by a
  verification pass that compares the result to the original;
* a container that is not what its extension claims is refused, not edited.

## Secrets

No command accepts an API token on the command line or reads one from the
config file. The config names an environment variable (`token_env`) or a
command that produces the token (`token_command`). A literal token in a
config file is rejected at load time. A redacting log filter is installed
unconditionally, so a token cannot reach a log file even through an
exception's representation of a request header.
