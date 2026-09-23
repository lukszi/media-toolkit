"""The server-side sub-commands, driven the way a person drives them.

The properties worth asserting are not about the output. They are: every
verb is registered, every verb that writes defaults to the dry run and has
exactly two states, and the exit code means something -- a refusal, a drifted
record and a failed precondition all exit non-zero, because these run in
pipelines where nobody is reading the output.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit import cli as jfkit_cli
from jfkit.commands import REGISTRARS, _fields_from

from tests.fake_server import (
    CREDENTIAL_VARIABLE,
    FIXTURE_CREDENTIAL,
    ITEMS,
    USER_ID,
    Recorder,
    fake_server,
)

FIRST = ITEMS[0]["Id"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


@pytest.fixture
def configured(
    server: tuple[str, Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A configuration file pointing at the stand-in, and a credential in the env."""
    url, _ = server
    config = tmp_path / "mediatoolkit.toml"
    config.write_text(
        "[server]\n"
        f'url = "{url}"\n'
        f'token_env = "{CREDENTIAL_VARIABLE}"\n'
        f'user_id = "{USER_ID}"\n'
        "\n[paths]\n"
        'movies = "/srv/media/movies"\n'
        'series = "/srv/media/series"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    return config


def run(config: Path, *argv: str) -> int:
    return jfkit_cli.main(["--config", str(config), *argv])


# ------------------------------------------------------------------ the shape
def test_every_verb_is_registered() -> None:
    parser = jfkit_cli.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    assert action.choices is not None
    assert set(action.choices) == {"naming", *REGISTRARS}


def _writing_parsers(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every parser under this one that has an --apply, however deeply nested.

    The first version of this walked the top level only, so the verbs that
    live under a group -- item set, libopts set, maintenance run, segments
    scope -- were invisible to it and an --apply removed from one of them
    would not have failed anything.
    """
    found: list[str] = []
    writes = False
    for action in parser._actions:
        if action.dest == "apply":
            assert action.default is False, prefix or "top level"
            writes = True
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for name, sub in choices.items():
                if isinstance(sub, argparse.ArgumentParser):
                    found += _writing_parsers(sub, f"{prefix} {name}".strip())
    if writes:
        found.append(prefix or "top level")
    return found


def test_every_writing_verb_has_two_states_and_defaults_to_the_dry_run() -> None:
    """There is no third state, and no environment variable that flips it."""
    writing = _writing_parsers(jfkit_cli.build_parser())
    for verb in ("item set", "libopts set", "maintenance run",
                 "maintenance previews", "segments scope", "segments cancel",
                 "refresh", "notify", "swap", "delete"):
        assert verb in writing, f"{verb} has no --apply"


def test_a_key_value_pair_keeps_the_type_it_looks_like() -> None:
    assert _fields_from(["IndexNumber=3"]) == {"IndexNumber": 3}
    assert _fields_from(["Name=Blue Canyon"]) == {"Name": "Blue Canyon"}
    assert _fields_from(["LockData=true"]) == {"LockData": True}
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        _fields_from(["Name"])


# ------------------------------------------------------------------ reading
def test_showing_an_item_prints_the_whole_record(
    configured: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert run(configured, "item", "show", FIRST, "--save",
               str(tmp_path / "before.json")) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed[printed.index("{"):])["Id"] == FIRST
    assert (tmp_path / "before.json").is_file()


def test_a_survey_prints_a_document_with_its_caveats(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(configured, "survey", "containers") == 0
    out = capsys.readouterr().out
    assert "# Container census" in out
    assert "## Caveats" in out


def test_a_survey_can_be_written_in_every_format(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(configured, "survey", "metadata", "--out", str(tmp_path / "out")) == 0
    written = sorted(p.suffix for p in (tmp_path / "out").iterdir())
    assert written == [".csv", ".html", ".json", ".md", ".tsv"]


def test_asking_which_device_backs_a_path_reads_nothing_else(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(configured, "jobs", "gate", str(tmp_path), "--ignore-server") == 0
    assert "clear" in capsys.readouterr().out


def test_lanes_are_printed_largest_first(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    small, large = tmp_path / "small.mkv", tmp_path / "large.mkv"
    small.write_bytes(b"x")
    large.write_bytes(b"x" * 100)
    assert run(configured, "jobs", "lanes", str(small), str(large)) == 0
    out = capsys.readouterr().out
    assert out.index("large.mkv") < out.index("small.mkv")


# ------------------------------------------------------------------ writing
def test_a_dry_run_set_sends_nothing(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    assert run(configured, "item", "set", FIRST, "--field", "Name=Blue Canyon") == 0
    assert recorder.posted == []
    assert "dry run" in capsys.readouterr().out


def test_apply_is_what_sends_it(
    configured: Path, server: tuple[str, Recorder]
) -> None:
    _url, recorder = server
    assert run(configured, "item", "set", FIRST,
               "--field", "Name=Blue Canyon", "--apply") == 0
    assert [item for item, _body in recorder.posted] == [FIRST]


def test_a_refresh_that_drifts_exits_non_zero(
    configured: Path, server: tuple[str, Recorder]
) -> None:
    """The exit code is the signal; nobody is reading the output."""
    _url, recorder = server
    recorder.refresh_effect[FIRST] = {"Name": "the container's own title"}
    assert run(configured, "refresh", FIRST, "--apply", "--poll", "0") == 1


def test_a_refresh_that_changes_only_what_was_expected_exits_zero(
    configured: Path, server: tuple[str, Recorder]
) -> None:
    _url, recorder = server
    recorder.refresh_effect[FIRST] = {"Overview": "A newly fetched description."}
    assert run(configured, "refresh", FIRST, "--expect", "Overview",
               "--apply", "--poll", "0") == 0


def test_notifying_a_library_root_is_refused_with_an_exit_code(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The roots come from the configuration, so this needs no extra switch."""
    _url, recorder = server
    assert run(configured, "notify", "/srv/media/movies", "--apply") == 2
    assert "library root" in capsys.readouterr().out
    assert recorder.notifications == []


def test_notifying_a_file_is_sent(
    configured: Path, server: tuple[str, Recorder]
) -> None:
    _url, recorder = server
    assert run(configured, "notify",
               "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv",
               "--apply") == 0
    assert len(recorder.notifications) == 1


def test_a_deletion_with_nothing_released_refuses_everything(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(
        "item_id\tpath\tcategory\n"
        f"{FIRST}\t{tmp_path / 'one.mkv'}\tbyte-identical-twin\n",
        encoding="utf-8",
    )
    code = run(configured, "delete", str(manifest), "--parked", str(tmp_path / "p"))
    assert code == 1
    assert "released: none" in capsys.readouterr().out


def test_the_database_verbs_work_without_a_server(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing about a local database needs the server to be reachable."""
    import sqlite3

    database = tmp_path / "catalogue.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE BaseItems (Id TEXT, Path TEXT)")
    connection.commit()
    connection.close()

    assert run(configured, "maintenance", "check", str(database)) == 0
    assert "ok" in capsys.readouterr().out
    assert run(configured, "maintenance", "snapshot", str(database),
               str(tmp_path / "copy.db")) == 0
    assert (tmp_path / "copy.db").is_file()


def test_restoring_previews_is_a_dry_run_by_default(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = tmp_path / "copy" / "one.trickplay"
    backup.mkdir(parents=True)
    (backup / "1.jpg").write_bytes(b"x")
    live = tmp_path / "live"
    live.mkdir()

    assert run(configured, "maintenance", "previews", str(tmp_path / "copy"),
               str(live)) == 0
    assert "would restore" in capsys.readouterr().out
    assert not (live / "one.trickplay").exists()


def test_a_library_with_a_stale_root_exits_non_zero(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    recorder.virtual_folders = [
        {"Name": "Movies", "ItemId": None, "LibraryOptions": None,
         "Path": "/var/lib/old-location/root/default/Movies"},
    ]
    assert run(configured, "libopts", "roots", "--data-dir",
               "/var/lib/media-server") == 1
    assert "cannot match" in capsys.readouterr().out


def test_the_options_document_prints_its_defaults_as_defaults(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "options.xml"
    document.write_text(
        "<LibraryOptions><EnableEmbeddedTitles>true</EnableEmbeddedTitles>"
        "</LibraryOptions>",
        encoding="utf-8",
    )
    assert run(configured, "libopts", "show", str(document)) == 0
    out = capsys.readouterr().out
    assert "EnableEmbeddedTitles = True" in out
    assert "(default)" in out


def test_listing_tasks_says_which_is_running(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    recorder.scheduled_tasks = [
        {"Id": "00000000-0000-0000-0000-000000000401", "Name": "Scan the library",
         "State": "Running"},
    ]
    assert run(configured, "segments", "tasks") == 0
    assert "<- running" in capsys.readouterr().out


def test_a_swap_plan_is_read_and_reported_without_moving_anything(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = tmp_path / "live" / "one.mkv"
    rebuilt = tmp_path / "staging" / "one.mkv"
    live.parent.mkdir(parents=True)
    rebuilt.parent.mkdir(parents=True)
    live.write_bytes(b"old")
    rebuilt.write_bytes(b"new and longer")

    plan = tmp_path / "plan.tsv"
    plan.write_text(f"{FIRST}\t{live}\t{rebuilt}\n", encoding="utf-8")

    assert run(configured, "swap", str(plan), "--parked", str(tmp_path / "parked")) == 0
    assert "nothing was moved" in capsys.readouterr().out
    assert live.read_bytes() == b"old"
