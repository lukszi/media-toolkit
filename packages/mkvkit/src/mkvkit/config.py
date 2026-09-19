"""mkvkit.config -- the one config model all three packages load.

One TOML file replaces every hardcoded path, id, drive letter and token.
Resolution order: --config PATH, $MEDIATOOLKIT_CONFIG, ./mediatoolkit.toml,
then the per-user location for the platform.

Parsed with tomllib into frozen dataclasses at start-up. Validation reports
EVERY missing or invalid key in one error rather than failing on the first.

Hard rule: a secret never has a value in the config file. The file names an
environment variable or a command that produces the secret, and a literal
token-shaped value is rejected at load time with an explanation.

The config object is passed explicitly. There is no module-level singleton
and nothing imports a token or a user id at import time.

Planned public API:
    load(path: Path | None = None) -> Config
    Config.server / .tools / .paths / .policy / .langid / .jobs
    resolve_secret(spec: SecretSpec) -> str

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
