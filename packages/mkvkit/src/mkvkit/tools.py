"""mkvkit.tools -- find the external programs, once, and say where they came from.

Resolution order, first hit wins::

    [tools] in the configuration file
    MKVKIT_<NAME> in the environment
    the PATH
    a per-platform fallback list

A resolution is cached and logged once at INFO with the version string the
program reports, so a bug report says which build produced the output. A
program that is nowhere raises :class:`ToolNotFound` listing every location
that was tried -- the useful half of the error.

No install directory is written into this file. The Windows fallbacks are
built from the environment variables the platform itself defines, so nothing
here names a drive, a profile or a machine; the ones that do not exist are
skipped. Anywhere else, a location somebody names is configuration.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .config import Config

__all__ = [
    "KNOWN_TOOLS",
    "ToolNotFound",
    "clear_cache",
    "find_tool",
    "search_locations",
    "tool_version",
]

log = logging.getLogger(__name__)

KNOWN_TOOLS = ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit", "mkvextract")

ENV_PREFIX = "MKVKIT_"

#: Directories under a Windows program-files or local-application root that
#: hold one of these programs in a default installation.
_WINDOWS_SUBDIRS = (
    ("MKVToolNix",),
    ("ffmpeg", "bin"),
    ("Jellyfin", "Server"),
)
_WINDOWS_ROOT_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA")

#: Directories a packaged installation uses on other platforms.
_POSIX_DIRS = (
    "/usr/lib/jellyfin-ffmpeg",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/snap/bin",
    "/opt/homebrew/bin",
)


class ToolNotFound(FileNotFoundError):
    """A program could not be found, and the error says where it was looked for."""

    def __init__(self, name: str, tried: list[str]) -> None:
        self.tool = name
        self.tried = tuple(tried)
        listed = "\n".join(f"  - {t}" for t in tried)
        super().__init__(
            f"{name} was not found. Set [tools].{name} in the configuration file, "
            f"or {ENV_PREFIX}{name.upper()} in the environment, or put it on the "
            f"PATH.\nTried:\n{listed}"
        )


_CACHE: dict[tuple[str, str | None, str | None], Path] = {}


def clear_cache() -> None:
    """Forget every resolution. Tests change the environment; long runs do not."""
    _CACHE.clear()


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def search_locations(
    name: str, *, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """The platform fallback directories that exist, in the order they are tried."""
    env = _env(environ)
    out: list[Path] = []
    if os.name == "nt":
        roots = []
        for var in _WINDOWS_ROOT_VARS:
            value = env.get(var)
            if value:
                roots.append(Path(value))
        for root in roots:
            for parts in _WINDOWS_SUBDIRS:
                candidate = root.joinpath(*parts)
                if candidate.is_dir() and candidate not in out:
                    out.append(candidate)
    else:
        for raw in _POSIX_DIRS:
            candidate = Path(raw)
            if candidate.is_dir() and candidate not in out:
                out.append(candidate)
    return out


def _executable_names(name: str) -> list[str]:
    return [f"{name}.exe", name] if os.name == "nt" else [name]


def find_tool(
    name: str,
    *,
    config: Config | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve one external program to an absolute path.

    >>> find_tool("ffmpeg")          # doctest: +SKIP
    PosixPath('/usr/bin/ffmpeg')
    """
    env = _env(environ)
    configured = config.tools.get(name) if config is not None else None
    from_env = env.get(f"{ENV_PREFIX}{name.upper()}")
    key = (name, configured, from_env)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    tried: list[str] = []
    for source, value in (("[tools] entry", configured), ("environment", from_env)):
        if not value:
            continue
        resolved = _resolve_named(value, env)
        tried.append(f"{source}: {value}")
        if resolved is not None:
            return _accept(key, name, resolved, source)

    for executable in _executable_names(name):
        found = shutil.which(executable, path=env.get("PATH"))
        if found:
            return _accept(key, name, Path(found), "PATH")
    tried.append("PATH")

    for directory in search_locations(name, environ=environ):
        tried.append(str(directory))
        for executable in _executable_names(name):
            candidate = directory / executable
            if candidate.is_file():
                return _accept(key, name, candidate, "platform fallback")

    raise ToolNotFound(name, tried)


def _resolve_named(value: str, env: Mapping[str, str]) -> Path | None:
    """A configured value is either an absolute path or a name on the PATH."""
    candidate = Path(value)
    if candidate.is_file():
        return candidate
    found = shutil.which(value, path=env.get("PATH"))
    return Path(found) if found else None


def _accept(
    key: tuple[str, str | None, str | None], name: str, path: Path, source: str
) -> Path:
    resolved = path.resolve()
    _CACHE[key] = resolved
    log.info("%s: %s (from the %s), %s", name, resolved, source, tool_version(resolved))
    return resolved


def tool_version(path: Path) -> str:
    """The first line the program prints for --version, or a plain 'unknown'.

    A version that cannot be read is not an error: it is one line of a log
    entry, and a program that runs is more interesting than one that
    introduces itself.
    """
    try:
        completed = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, check=False,
        )
    except OSError:
        return "version unknown"
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0].strip() if output else "version unknown"
