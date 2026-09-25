"""mkvkit.config -- the one configuration model all three packages load.

One TOML file replaces every hardcoded path, identifier, drive and token.
It is found in this order::

    --config PATH  ->  $MEDIATOOLKIT_CONFIG  ->  ./mediatoolkit.toml  ->  per-user

The per-user location is the platform one: the roaming application-data
directory on Windows, ``$XDG_CONFIG_HOME`` (or ``~/.config``) elsewhere, both
under ``media-toolkit/config.toml``.

Three rules the rest of the project depends on.

**A secret never has a value in the file.** The file names an environment
variable (``token_env``) or a command that prints the secret
(``token_command``); a literal is refused at load time with an explanation,
not read and ignored, because a value that reached a file once will reach a
backup and a screenshot too.

**Every problem is reported at once.** Validation collects a list and raises
one error naming every missing, unknown or invalid key, because fixing a
configuration file one traceback at a time is how people give up.

**The result is passed explicitly.** There is no module-level singleton and no
import that hands its importer a credential. That is the direct fix for a
layout where every module imported a token and a user id by importing a
client.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

#: Anything that turns a command line into its output. Injected in tests, so
#: no test ever runs a password manager.
CommandRunner = Callable[[str], str]

__all__ = [
    "CONFIG_ENV",
    "DEFAULT_FILE_NAME",
    "CommandRunner",
    "Config",
    "ConfigError",
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
    "loads",
    "user_config_path",
]

DEFAULT_FILE_NAME = "mediatoolkit.toml"
CONFIG_ENV = "MEDIATOOLKIT_CONFIG"
APP_DIR_NAME = "media-toolkit"

#: Keys that must never carry a value. The indirection is the whole point.
FORBIDDEN_SECRET_KEYS = frozenset(
    {"token", "api_key", "apikey", "key", "secret", "password", "passwd"}
)

_GUID = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_LANG = re.compile(r"\A[a-z]{2,3}\Z")


class ConfigError(ValueError):
    """Every problem found in one file, in one error.

    ``problems`` is the full list; ``str(exc)`` prints all of them.
    """

    def __init__(self, problems: Iterable[str], source: Path | None = None) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        self.source = source
        where = str(source) if source is not None else "the configuration"
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{len(self.problems)} problem(s) in {where}:\n{body}")


class SecretUnavailable(ConfigError):
    """The indirection is configured, but it did not produce a value."""


# --------------------------------------------------------------------- sections
@dataclass(frozen=True)
class ServerConfig:
    """Where the media server is, and where its token comes from -- not what it is."""

    url: str = "http://127.0.0.1:8096"
    token_env: str | None = None
    token_command: str | None = None
    user_id: str | None = None
    data_dir: Path | None = None

    def token(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
    ) -> str:
        """Resolve the token through whichever indirection is configured.

        Raises :class:`SecretUnavailable` naming the variable or the command,
        never printing what it found.
        """
        env = os.environ if environ is None else environ
        if self.token_env:
            value = env.get(self.token_env, "")
            if not value:
                raise SecretUnavailable(
                    [f"server.token_env names {self.token_env}, which is unset or empty"]
                )
            return value
        if self.token_command:
            run = runner if runner is not None else _run_command
            out = run(self.token_command)
            value = out.splitlines()[0].strip() if out.strip() else ""
            if not value:
                raise SecretUnavailable(
                    ["server.token_command produced no output on its first line"]
                )
            return value
        raise SecretUnavailable(
            ["server needs token_env or token_command; a token is never a value here"]
        )


@dataclass(frozen=True)
class ToolsConfig:
    """Explicit locations for external programs. Empty means "look for them"."""

    entries: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def get(self, name: str) -> str | None:
        return self.entries.get(name)


@dataclass(frozen=True)
class PathsConfig:
    movies: Path | None = None
    series: Path | None = None
    staging: Path | None = None
    parked: Path | None = None
    work: Path = Path("work")


@dataclass(frozen=True)
class PolicyConfig:
    keep_languages: tuple[str, ...] = ()
    droppable_languages: tuple[str, ...] = ()
    default_audio: str | None = None


@dataclass(frozen=True)
class LangidConfig:
    """Fitted defaults, not universals. See the method document before trusting them."""

    model: str = "large-v3"
    device: str = "auto"
    min_conf: float = 0.92
    override_conf: float = 0.97
    #: Where the speech model's files are cached. None is the library's own
    #: default cache.
    model_dir: Path | None = None


@dataclass(frozen=True)
class JobsConfig:
    device_gate: bool = True
    max_readers_per_device: int = 1


@dataclass(frozen=True)
class Config:
    source: Path | None = None
    server: ServerConfig = field(default_factory=ServerConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    langid: LangidConfig = field(default_factory=LangidConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)


def _run_command(command: str) -> str:
    """Run a token command without a shell, and return its output."""
    argv = shlex.split(command, posix=os.name != "nt")
    if not argv:
        raise SecretUnavailable(["server.token_command is empty"])
    argv = [a.strip('"') for a in argv]
    completed = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SecretUnavailable(
            [f"server.token_command exited {completed.returncode}"]
        )
    return completed.stdout


# ------------------------------------------------------------------- discovery
def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if sys.platform == "win32":
        base = env.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = env.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME / "config.toml"


def candidate_paths(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> list[Path]:
    """The resolution order, as data, so it can be printed and tested."""
    env = os.environ if environ is None else environ
    here = Path.cwd() if cwd is None else cwd
    out: list[Path] = []
    if explicit is not None:
        out.append(Path(explicit))
    from_env = env.get(CONFIG_ENV)
    if from_env:
        out.append(Path(from_env))
    out.append(here / DEFAULT_FILE_NAME)
    out.append(user_config_path(env))
    return out


def discover(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path | None:
    """The first candidate that exists, or None when the defaults will do."""
    for candidate in candidate_paths(explicit, environ=environ, cwd=cwd):
        if candidate.is_file():
            return candidate
    return None


def load(
    explicit: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Config:
    """Find, parse and validate the configuration file.

    A file named explicitly and missing is an error; no file at all is not --
    the defaults are usable and nothing in them points anywhere real.
    """
    if explicit is not None and not Path(explicit).is_file():
        raise ConfigError([f"no such configuration file: {explicit}"])
    path = discover(explicit, environ=environ, cwd=cwd)
    if path is None:
        return Config()
    return loads(path.read_text(encoding="utf-8"), source=path)


def loads(text: str, *, source: Path | None = None) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"not valid TOML: {exc}"], source) from exc
    problems: list[str] = []
    known = {"server", "tools", "paths", "policy", "langid", "jobs"}
    for name in raw:
        if name not in known:
            problems.append(f"unknown section [{name}]")
    reader = _Reader(raw, problems)
    server = _server(reader)
    tools = _tools(reader)
    paths = _paths(reader)
    policy = _policy(reader)
    langid = _langid(reader)
    jobs = _jobs(reader)
    if problems:
        raise ConfigError(problems, source)
    return Config(
        source=source, server=server, tools=tools, paths=paths,
        policy=policy, langid=langid, jobs=jobs,
    )


# ------------------------------------------------------------------ validation
class _Reader:
    """Collects problems instead of raising, so one error can name them all."""

    def __init__(self, raw: Mapping[str, object], problems: list[str]) -> None:
        self.raw = raw
        self.problems = problems

    def section(self, name: str, keys: Sequence[str]) -> Mapping[str, object]:
        value = self.raw.get(name, {})
        if not isinstance(value, dict):
            self.problems.append(f"[{name}] must be a table")
            return {}
        for key in value:
            if key in FORBIDDEN_SECRET_KEYS:
                self.problems.append(
                    f"{name}.{key} must not hold a value: name an environment "
                    f"variable with token_env, or a command with token_command"
                )
            elif key not in keys:
                self.problems.append(f"unknown key {name}.{key}")
        return value

    def string(self, sec: str, data: Mapping[str, object], key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{sec}.{key} must be a non-empty string")
            return None
        return value

    def path(self, sec: str, data: Mapping[str, object], key: str) -> Path | None:
        value = self.string(sec, data, key)
        return Path(value) if value is not None else None

    def number(self, sec: str, data: Mapping[str, object], key: str) -> float | None:
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.problems.append(f"{sec}.{key} must be a number")
            return None
        return float(value)

    def integer(self, sec: str, data: Mapping[str, object], key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.problems.append(f"{sec}.{key} must be an integer")
            return None
        return value

    def boolean(self, sec: str, data: Mapping[str, object], key: str) -> bool | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            self.problems.append(f"{sec}.{key} must be true or false")
            return None
        return value

    def languages(self, sec: str, data: Mapping[str, object], key: str) -> tuple[str, ...]:
        value = data.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.problems.append(f"{sec}.{key} must be a list of language codes")
            return ()
        codes = tuple(str(v) for v in value)
        for code in codes:
            if not _LANG.match(code):
                self.problems.append(
                    f"{sec}.{key}: {code!r} is not a two- or three-letter code"
                )
        return codes


def _server(reader: _Reader) -> ServerConfig:
    keys = ("url", "token_env", "token_command", "user_id", "data_dir")
    data = reader.section("server", keys)
    url = reader.string("server", data, "url") or ServerConfig.url
    if not url.startswith(("http://", "https://")):
        reader.problems.append("server.url must start with http:// or https://")
    elif "@" in urlsplit(url).netloc:
        # A credential in the address would be printed wherever the address
        # is, which is every log line about the server. The message does not
        # repeat the address, for the same reason.
        reader.problems.append(
            "server.url must not carry a user name or password (user:pass@host); "
            "give the address alone and the token through token_env or token_command"
        )
    token_env = reader.string("server", data, "token_env")
    token_command = reader.string("server", data, "token_command")
    if token_env and token_command:
        reader.problems.append(
            "server: set token_env or token_command, not both"
        )
    user_id = reader.string("server", data, "user_id")
    if user_id is not None and not _GUID.match(user_id):
        reader.problems.append("server.user_id must be a globally unique identifier")
    return ServerConfig(
        url=url, token_env=token_env, token_command=token_command,
        user_id=user_id, data_dir=reader.path("server", data, "data_dir"),
    )


def _tools(reader: _Reader) -> ToolsConfig:
    known = ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit", "mkvextract")
    data = reader.section("tools", known)
    entries: dict[str, str] = {}
    for name in known:
        value = reader.string("tools", data, name)
        if value is not None:
            entries[name] = value
    return ToolsConfig(MappingProxyType(entries))


def _paths(reader: _Reader) -> PathsConfig:
    keys = ("movies", "series", "staging", "parked", "work")
    data = reader.section("paths", keys)
    work = reader.path("paths", data, "work")
    return PathsConfig(
        movies=reader.path("paths", data, "movies"),
        series=reader.path("paths", data, "series"),
        staging=reader.path("paths", data, "staging"),
        parked=reader.path("paths", data, "parked"),
        work=work if work is not None else PathsConfig.work,
    )


def _policy(reader: _Reader) -> PolicyConfig:
    keys = ("keep_languages", "droppable_languages", "default_audio")
    data = reader.section("policy", keys)
    keep = reader.languages("policy", data, "keep_languages")
    drop = reader.languages("policy", data, "droppable_languages")
    both = sorted(set(keep) & set(drop))
    if both:
        reader.problems.append(
            f"policy: {', '.join(both)} listed as both kept and droppable"
        )
    default_audio = reader.string("policy", data, "default_audio")
    if default_audio is not None and not _LANG.match(default_audio):
        reader.problems.append("policy.default_audio is not a language code")
    return PolicyConfig(keep, drop, default_audio)


def _langid(reader: _Reader) -> LangidConfig:
    keys = ("model", "device", "min_conf", "override_conf", "model_dir")
    data = reader.section("langid", keys)
    model = reader.string("langid", data, "model") or LangidConfig.model
    device = reader.string("langid", data, "device") or LangidConfig.device
    if device not in {"auto", "cpu", "cuda"}:
        reader.problems.append("langid.device must be auto, cpu or cuda")
    min_conf = reader.number("langid", data, "min_conf")
    override_conf = reader.number("langid", data, "override_conf")
    min_conf = LangidConfig.min_conf if min_conf is None else min_conf
    override_conf = LangidConfig.override_conf if override_conf is None else override_conf
    for name, value in (("min_conf", min_conf), ("override_conf", override_conf)):
        if not 0.0 < value <= 1.0:
            reader.problems.append(f"langid.{name} must be above 0 and at most 1")
    if override_conf < min_conf:
        reader.problems.append(
            "langid.override_conf must be at least min_conf: overturning an existing "
            "tag is held to a higher bar than confirming one"
        )
    return LangidConfig(
        model, device, min_conf, override_conf,
        model_dir=reader.path("langid", data, "model_dir"),
    )


def _jobs(reader: _Reader) -> JobsConfig:
    keys = ("device_gate", "max_readers_per_device")
    data = reader.section("jobs", keys)
    gate = reader.boolean("jobs", data, "device_gate")
    readers = reader.integer("jobs", data, "max_readers_per_device")
    if readers is not None and readers < 1:
        reader.problems.append("jobs.max_readers_per_device must be at least 1")
        readers = None
    return JobsConfig(
        device_gate=JobsConfig.device_gate if gate is None else gate,
        max_readers_per_device=(
            JobsConfig.max_readers_per_device if readers is None else readers
        ),
    )
