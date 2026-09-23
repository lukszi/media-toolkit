"""The configuration model: discovery, indirection, and one error for everything.

The interesting assertions here are the refusals. A configuration model that
accepts a literal secret is a configuration model that puts one in a backup.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.config import (
    CONFIG_ENV,
    Config,
    ConfigError,
    SecretUnavailable,
    ServerConfig,
    candidate_paths,
    discover,
    load,
    loads,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "mediatoolkit.example.toml"

MINIMAL = """
[server]
url = "http://127.0.0.1:8096"
token_env = "JFKIT_TOKEN"
"""


def test_defaults_point_nowhere_real() -> None:
    config = Config()
    assert config.source is None
    assert config.server.token_env is None and config.server.token_command is None
    assert config.paths.movies is None and config.paths.parked is None
    assert config.paths.work == Path("work")
    assert config.jobs.max_readers_per_device == 1


def test_the_example_file_still_validates() -> None:
    """The shipped example is loaded by the suite, so it cannot rot."""
    config = loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"), source=EXAMPLE_CONFIG)
    assert config.server.token_env
    assert config.policy.keep_languages == ("eng", "deu")
    assert config.langid.override_conf >= config.langid.min_conf
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "token =" not in text, "the example must never carry a literal"


def test_a_literal_secret_is_refused_with_an_explanation() -> None:
    with pytest.raises(ConfigError) as caught:
        loads('[server]\ntoken = "literal"\n')
    message = str(caught.value)
    assert "token_env" in message and "token_command" in message


@pytest.mark.parametrize("key", ["api_key", "secret", "password"])
def test_every_secret_shaped_key_is_refused(key: str) -> None:
    with pytest.raises(ConfigError):
        loads(f'[server]\n{key} = "literal"\n')


def test_every_problem_is_reported_at_once() -> None:
    bad = """
[server]
url = "ftp://example.com"
user_id = "not-an-identifier"

[langid]
min_conf = 0.9
override_conf = 0.5

[nonsense]
x = 1
"""
    with pytest.raises(ConfigError) as caught:
        loads(bad)
    problems = caught.value.problems
    assert len(problems) == 4, problems
    assert any("server.url" in p for p in problems)
    assert any("user_id" in p for p in problems)
    assert any("override_conf" in p for p in problems)
    assert any("nonsense" in p for p in problems)


def test_unknown_keys_are_named_not_ignored() -> None:
    with pytest.raises(ConfigError) as caught:
        loads('[paths]\nstagingg = "/srv/staging"\n')
    assert "paths.stagingg" in str(caught.value)


def test_both_indirections_at_once_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        loads('[server]\ntoken_env = "A"\ntoken_command = "b"\n')
    assert "not both" in str(caught.value)


def test_token_comes_from_the_environment() -> None:
    server = ServerConfig(token_env="JFKIT_TOKEN")
    assert server.token(environ={"JFKIT_TOKEN": "value-from-env"}) == "value-from-env"


def test_an_unset_variable_names_itself() -> None:
    server = ServerConfig(token_env="JFKIT_TOKEN")
    with pytest.raises(SecretUnavailable) as caught:
        server.token(environ={})
    assert "JFKIT_TOKEN" in str(caught.value)


def test_token_comes_from_a_command_first_line_only() -> None:
    server = ServerConfig(token_command="printer --quiet")
    calls: list[str] = []

    def runner(command: str) -> str:
        calls.append(command)
        return "value-from-command\ntrailing noise\n"

    assert server.token(runner=runner) == "value-from-command"
    assert calls == ["printer --quiet"]


def test_no_indirection_at_all_says_so() -> None:
    with pytest.raises(SecretUnavailable):
        ServerConfig().token(environ={})


def test_discovery_order(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    from_env = tmp_path / "from-env.toml"
    in_cwd = tmp_path / "cwd"
    in_cwd.mkdir()
    order = candidate_paths(explicit, environ={CONFIG_ENV: str(from_env)}, cwd=in_cwd)
    assert order[0] == explicit
    assert order[1] == from_env
    assert order[2] == in_cwd / "mediatoolkit.toml"
    assert order[3].name == "config.toml"


def test_discovery_picks_the_first_that_exists(tmp_path: Path) -> None:
    present = tmp_path / "mediatoolkit.toml"
    present.write_text(MINIMAL, encoding="utf-8")
    assert discover(environ={}, cwd=tmp_path) == present
    assert discover(environ={}, cwd=tmp_path / "elsewhere") is None


def test_load_reads_the_discovered_file(tmp_path: Path) -> None:
    (tmp_path / "mediatoolkit.toml").write_text(MINIMAL, encoding="utf-8")
    config = load(environ={}, cwd=tmp_path)
    assert config.source == tmp_path / "mediatoolkit.toml"
    assert config.server.token_env == "JFKIT_TOKEN"


def test_no_file_anywhere_is_not_an_error(tmp_path: Path) -> None:
    assert load(environ={}, cwd=tmp_path / "nothing-here") == Config()


def test_a_named_file_that_is_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path / "absent.toml", environ={}, cwd=tmp_path)


def test_language_codes_are_checked() -> None:
    with pytest.raises(ConfigError) as caught:
        loads('[policy]\nkeep_languages = ["english"]\n')
    assert "keep_languages" in str(caught.value)


def test_a_language_cannot_be_kept_and_dropped() -> None:
    with pytest.raises(ConfigError) as caught:
        loads('[policy]\nkeep_languages = ["eng"]\ndroppable_languages = ["eng"]\n')
    assert "both kept and droppable" in str(caught.value)


def test_the_server_side_shares_one_model() -> None:
    """There is one configuration model, not two that drift apart."""
    import jfkit.config as server_side

    assert server_side.Config is Config
    assert server_side.load(environ={}, cwd=Path("nowhere-at-all")) == Config()


def test_the_server_side_insists_on_an_indirection() -> None:
    from jfkit.config import require_server

    with pytest.raises(ConfigError) as caught:
        require_server(loads('[server]\nurl = "http://127.0.0.1:8096"\n'))
    assert "token_env" in str(caught.value)
    assert require_server(loads(MINIMAL)).token_env == "JFKIT_TOKEN"


def test_the_file_side_does_not_have_to_describe_a_server() -> None:
    """A tool that never talks to a server must not be forced to configure one."""
    config = loads('[paths]\nwork = "./work"\n')
    assert config.server.token_env is None
