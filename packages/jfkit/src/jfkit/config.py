"""jfkit.config -- the same configuration model, with the server section required.

There is one model, and it lives in :mod:`mkvkit.config`: one file, one
resolution order, one validator that reports every problem at once. This
module re-exports it so a server-side caller never imports the file-side
package by hand, and adds the two requirements that only apply here -- a
server address, and an indirection that can actually produce a token.

Nothing in this module holds a secret. ``Config`` is passed explicitly to the
client that needs one, and the token is resolved at the moment it is used.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from mkvkit.config import (
    CONFIG_ENV,
    DEFAULT_FILE_NAME,
    CommandRunner,
    Config,
    ConfigError,
    DedupePolicy,
    JobsConfig,
    LangidConfig,
    PathsConfig,
    PolicyConfig,
    SecretUnavailable,
    ServerConfig,
    ToolsConfig,
    candidate_paths,
    discover,
    user_config_path,
)
from mkvkit.config import load as load_config

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_FILE_NAME",
    "CommandRunner",
    "Config",
    "ConfigError",
    "DedupePolicy",
    "JobsConfig",
    "LangidConfig",
    "PathsConfig",
    "PolicyConfig",
    "SecretUnavailable",
    "ServerConfig",
    "ToolsConfig",
    "candidate_paths",
    "discover",
    "load",
    "load_config",
    "require_server",
    "user_config_path",
]


def require_server(config: Config) -> ServerConfig:
    """Fail early, and in one message, when the server section is unusable.

    The check is separate from parsing because the file-side tools load the
    same file and must not be forced to describe a server they never talk to.
    """
    problems: list[str] = []
    if not config.server.url:
        problems.append("server.url is required to talk to a media server")
    if not (config.server.token_env or config.server.token_command):
        problems.append(
            "server needs token_env or token_command: the token itself is never "
            "a value in this file"
        )
    if problems:
        raise ConfigError(problems, config.source)
    return config.server


def load(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    require: bool = False,
) -> Config:
    """Load the shared configuration; with ``require``, insist it names a server."""
    config = load_config(explicit, environ=environ, cwd=cwd)
    if require:
        require_server(config)
    return config
