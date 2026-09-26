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
import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit import cli as jfkit_cli
from jfkit import client as jfkit_client
from jfkit.commands import REGISTRARS, _fields_from
from jfkit.verbs import REGISTRARS as READ_VERBS

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
    assert set(action.choices) == {"naming", *REGISTRARS, *READ_VERBS}


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
    printed = capsys.readouterr()
    assert json.loads(printed.out)["Id"] == FIRST, "stdout is the record alone"
    assert "written to" in printed.err
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
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The machine's real process table is not this test's to depend on: any
    # program reading the temporary directory's device -- another test of this
    # run, working on media in parallel, included -- would hold the gate. The
    # real listing is checked on its own in test_jobs.py.
    monkeypatch.setattr("jfkit.jobs.list_processes", lambda: [])
    assert run(configured, "jobs", "gate", str(tmp_path), "--ignore-server") == 0
    assert "clear" in capsys.readouterr().out


def test_the_gate_verb_is_held_by_a_reader_on_that_device(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jfkit.devices import device_of
    from jfkit.jobs import Process

    reader = Process(pid=7, name="ffmpeg", command=f"ffmpeg -i {tmp_path / 'a.mkv'}")
    monkeypatch.setattr("jfkit.jobs.list_processes", lambda: [reader])
    assert run(configured, "jobs", "gate", str(tmp_path), "--ignore-server") == 1
    out = capsys.readouterr().out
    assert f"{device_of(tmp_path)}: held" in out


def test_lanes_are_printed_largest_first(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    small, large = tmp_path / "small.mkv", tmp_path / "large.mkv"
    small.write_bytes(b"x")
    large.write_bytes(b"x" * 100)
    assert run(configured, "jobs", "lanes", str(small), str(large)) == 0
    out = capsys.readouterr().out
    assert out.index("large.mkv") < out.index("small.mkv")


def test_naming_says_what_its_exit_status_means(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        jfkit_cli.main(["naming", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "exit status: 0 when no name would be read as an episode range" in text


def test_naming_reads_a_list_and_prints_what_it_was_given(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hundreds of names do not fit on one command line; a list does."""
    names = [f"/srv/media/series/Harbour Lights/Season 01/Harbour Lights - S01E{n:02d}.mkv"
             for n in range(1, 41)]
    listing = tmp_path / "names.txt"
    listing.write_text("\n".join(names[:20]) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(names[20:]) + "\n"))
    assert run(configured, "naming", f"@{listing}", "-", "--full-paths") == 0
    printed = capsys.readouterr().out
    assert all(name in printed for name in names)
    assert "40 path(s)" in printed


def test_naming_warns_when_the_servers_own_files_would_not_fit(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name parses; the preview tiles the server writes beside it do not fit."""
    folder = "\\".join(["srv", "media", "series", "Harbour Lights " + "x" * 185, "Season 01"])
    fits = folder + "\\Harbour Lights - S01E01.mkv"
    assert len(fits) < 259
    assert run(configured, "naming", fits, "--only-problems") == 0
    printed = capsys.readouterr().out
    assert "LONG" in printed and "1 too long with their sidecars" in printed


def test_notify_reads_its_paths_from_standard_input(
    configured: Path, server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    _url, recorder = server
    season = "/srv/media/series/Harbour Lights/Season 01"
    monkeypatch.setattr("sys.stdin", io.StringIO(
        f"{season}/Harbour Lights - S01E01.mkv\n\n{season}/Harbour Lights - S01E02.mkv\n"
    ))
    assert run(configured, "notify", "-", "--apply") == 0
    assert [u["Path"].replace("\\", "/") for u in recorder.notifications] == [
        f"{season}/Harbour Lights - S01E01.mkv", f"{season}/Harbour Lights - S01E02.mkv",
    ]


# ------------------------------------------------------------------ writing
def test_a_dry_run_set_sends_nothing(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    assert run(configured, "item", "set", FIRST, "--field", "Name=Blue Canyon") == 0
    assert recorder.posted == []
    assert "dry run" in capsys.readouterr().out


def test_a_dry_run_set_reports_the_field_and_not_the_record(
    configured: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-field change is reported as that field, before and after.

    The body the dry run would send is the whole record, several kilobytes on
    one line on a real server; above the limit it is summarised by its size.
    The fixture record is small, so the limit is lowered to meet it.
    """
    monkeypatch.setattr(jfkit_client, "DRY_RUN_BODY_LIMIT", 16)
    assert run(configured, "item", "set", FIRST,
               "--field", "ParentIndexNumber=1") == 0
    printed = capsys.readouterr()
    assert "ParentIndexNumber: null -> 1" in printed.out
    assert "ParentIndexNumber: null -> 1" in printed.err
    assert "-v prints it" in printed.err
    assert f'"Id": "{FIRST}"' not in printed.err


def test_the_whole_dry_run_body_is_one_verbosity_level_away(
    configured: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jfkit_client, "DRY_RUN_BODY_LIMIT", 16)
    assert run(configured, "-v", "item", "set", FIRST,
               "--field", "ParentIndexNumber=1") == 0
    assert f'"Id": "{FIRST}"' in capsys.readouterr().err


def test_apply_is_what_sends_it(
    configured: Path, server: tuple[str, Recorder], tmp_path: Path
) -> None:
    _url, recorder = server
    backup = tmp_path / "before" / "first.json"
    assert run(configured, "item", "set", FIRST,
               "--field", "Name=Blue Canyon", "--backup", str(backup),
               "--apply") == 0
    assert [item for item, _body in recorder.posted] == [FIRST]
    assert json.loads(backup.read_text(encoding="utf-8"))["Name"] == ITEMS[0]["Name"]


def test_an_applied_set_without_a_backup_is_refused(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    assert run(configured, "item", "set", FIRST,
               "--field", "Name=Blue Canyon", "--apply") == 2
    assert "--backup PATH" in capsys.readouterr().out
    assert recorder.posted == []
    assert not any(method == "POST" for method, _route in recorder.requests)


def test_an_applied_options_write_without_a_backup_directory_is_refused(
    configured: Path, server: tuple[str, Recorder], tmp_path: Path,
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    document = tmp_path / "options.xml"
    document.write_text(
        "<LibraryOptions><EnableEmbeddedTitles>true</EnableEmbeddedTitles>"
        "</LibraryOptions>",
        encoding="utf-8",
    )
    assert run(configured, "libopts", "set", str(document), "--id", FIRST,
               "--field", "EnableEmbeddedTitles=false", "--apply") == 2
    assert "--backup-dir DIR" in capsys.readouterr().out
    assert recorder.options_writes == []


def test_an_applied_options_write_copies_the_document_first(
    configured: Path, server: tuple[str, Recorder], tmp_path: Path
) -> None:
    _url, recorder = server
    document = tmp_path / "options.xml"
    document.write_text(
        "<LibraryOptions><EnableEmbeddedTitles>true</EnableEmbeddedTitles>"
        "</LibraryOptions>",
        encoding="utf-8",
    )
    recorder.options_documents[FIRST] = document
    run(configured, "libopts", "set", str(document), "--id", FIRST,
        "--field", "EnableEmbeddedTitles=false",
        "--backup-dir", str(tmp_path / "backups"), "--apply")
    [copy] = (tmp_path / "backups").glob("*.xml")
    assert "<EnableEmbeddedTitles>true" in copy.read_text(encoding="utf-8")
    assert len(recorder.options_writes) == 1


def test_an_applied_scope_without_a_backup_directory_is_refused(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    assert run(configured, "segments", "scope", "--plugin", "segment",
               "--apply") == 2
    assert "--backup-dir DIR" in capsys.readouterr().out
    assert recorder.requests == [], "refused before the server was asked anything"


def test_an_applied_scope_writes_the_configuration_as_it_was(
    configured: Path, server: tuple[str, Recorder], tmp_path: Path
) -> None:
    _url, recorder = server
    plugin_id = "00000000-0000-0000-0000-000000000301"
    recorder.installed_plugins = [{"Id": plugin_id, "Name": "Segment Finder"}]
    recorder.plugin_configuration[plugin_id] = {
        "SeriesExclusions": ["kept"], "MovieExclusions": [],
    }
    assert run(configured, "segments", "scope", "--plugin", "segment",
               "--backup-dir", str(tmp_path / "backups"), "--apply") == 0
    [copy] = (tmp_path / "backups").glob("*.json")
    assert json.loads(copy.read_text(encoding="utf-8"))["SeriesExclusions"] == ["kept"]
    assert recorder.plugin_configuration[plugin_id]["SeriesExclusions"][0] == "kept"


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


def test_a_refresh_whose_expected_field_did_not_change_exits_non_zero(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    recorder.refresh_effect[FIRST] = {"DateLastRefreshed": "2026-01-02T03:04:05Z"}
    assert run(configured, "refresh", FIRST, "--expect", "ParentIndexNumber",
               "--apply", "--poll", "0") == 1
    assert "expected field ParentIndexNumber: unchanged" in capsys.readouterr().out


def test_notifying_a_library_root_is_refused_with_an_exit_code(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The roots come from the configuration, so this needs no extra switch."""
    _url, recorder = server
    assert run(configured, "notify", "/srv/media/movies", "--apply") == 2
    assert "library root" in capsys.readouterr().out
    assert recorder.notifications == []


def test_a_library_root_only_the_server_knows_is_refused_too(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    """A library nobody put in the configuration is a root all the same."""
    _url, recorder = server
    recorder.virtual_folders = [
        {"Name": "Documentaries", "Locations": ["/srv/other/documentaries"]},
    ]
    assert run(configured, "notify", "/srv/other/documentaries", "--apply") == 2
    assert run(configured, "notify", "/srv/other", "--apply") == 2
    assert "library root" in capsys.readouterr().out
    assert recorder.notifications == []


def test_a_notification_is_not_sent_when_the_libraries_cannot_be_listed(
    configured: Path, server: tuple[str, Recorder],
    capsys: pytest.CaptureFixture[str]
) -> None:
    _url, recorder = server
    recorder.virtual_folders_status = 500
    assert run(configured, "notify",
               "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv",
               "--apply") == 2
    assert "could not be read" in capsys.readouterr().out
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



def _database(path: Path) -> Path:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE BaseItems (Id TEXT, Path TEXT)")
    connection.commit()
    connection.close()
    return path


def test_a_maintenance_pass_needs_no_server_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no server described, the busy check is skipped, not demanded."""
    config = tmp_path / "files-only.toml"
    config.write_text('[paths]\nmovies = "/srv/media/movies"\n', encoding="utf-8")
    database = _database(tmp_path / "catalogue.db")
    assert run(config, "maintenance", "run", str(database), "--reindex") == 0
    assert "dry run, nothing written" in capsys.readouterr().out


def test_a_named_controller_without_a_service_name_is_refused(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path / "catalogue.db")
    before = database.read_bytes()
    assert run(configured, "maintenance", "run", str(database), "--reindex",
               "--service", "systemd", "--snapshot-dir", str(tmp_path / "copies"),
               "--apply") == 2
    assert "--service-name" in capsys.readouterr().out
    assert database.read_bytes() == before
    assert not (tmp_path / "copies").exists()


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
    configured: Path, server: tuple[str, Recorder], tmp_path: Path,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.stand_ins import payload_is_a_stand_in

    payload_is_a_stand_in(monkeypatch)  # the "rebuild" here is a few bytes of text
    _, recorder = server
    live = tmp_path / "live" / "one.mkv"
    rebuilt = tmp_path / "staging" / "one.mkv"
    live.parent.mkdir(parents=True)
    rebuilt.parent.mkdir(parents=True)
    live.write_bytes(b"old")
    rebuilt.write_bytes(b"new and longer")
    recorder.items[0]["Path"] = str(live)

    plan = tmp_path / "plan.tsv"
    plan.write_text(f"{FIRST}\t{live}\t{rebuilt}\t2\n", encoding="utf-8")

    assert run(configured, "swap", str(plan), "--parked", str(tmp_path / "parked")) == 0
    assert "nothing was moved" in capsys.readouterr().out
    assert live.read_bytes() == b"old"

    # the dry run reads the preconditions: a plan naming another file fails
    recorder.items[0]["Path"] = "/srv/media/movies/Blue Canyon (1998)/keeper.mkv"
    assert run(configured, "swap", str(plan), "--parked", str(tmp_path / "parked")) == 1
    assert "the plan's path is not the item's" in capsys.readouterr().out
    assert live.read_bytes() == b"old"


def test_naming_does_not_walk_into_a_link_and_honours_excludes(
    configured: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from tests.test_walk import _junction

    elsewhere = tmp_path / "another-disk"
    elsewhere.mkdir()
    (elsewhere / "Harbour.Lights.S01E02.mkv").write_bytes(b"x")
    library = tmp_path / "library"
    (library / "Skip").mkdir(parents=True)
    (library / "Northwind.S01E03E04.mkv").write_bytes(b"x")
    (library / "Skip" / "Harbour.Lights.S01E02.mkv").write_bytes(b"x")
    if sys.platform == "win32":
        _junction(elsewhere, library / "linked")
    else:
        (library / "linked").symlink_to(elsewhere, target_is_directory=True)
    run(configured, "naming", str(library), "--exclude", "Skip")
    out = capsys.readouterr().out
    assert "Northwind.S01E03E04.mkv" in out
    assert "Harbour.Lights.S01E02.mkv" not in out
    assert "1 path(s)" in out
