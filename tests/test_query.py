"""find, children and playstate: the read verbs, user-scoped and in rows."""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit import cli as jfkit_cli
from jfkit.query import (
    ITEM_COLUMNS,
    PLAYSTATE_COLUMNS,
    children,
    find,
    item_row,
    playstate,
    render,
)

from tests.fake_server import (
    CREDENTIAL_VARIABLE,
    FIXTURE_CREDENTIAL,
    SECOND_USER_ID,
    THIRD_USER_ID,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
    series_tree,
)

TREE = series_tree("Harbour Lights", {1: 3, 2: 2}, first=100)
SERIES = TREE[0]["Id"]
SEASON_ONE = TREE[1]["Id"]
EPISODES = [row["Id"] for row in TREE if row["Type"] == "Episode"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        running[1].items += [dict(row) for row in TREE]
        yield running


def test_find_by_name_is_user_scoped(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    found = find(client_for(url), name="harbour lights", exact=True, types=["Series"])
    assert [row["Id"] for row in found] == [SERIES]
    assert all(route.startswith("/Users/") for _m, route in recorder.requests)


def test_find_by_path_takes_a_folder_or_a_glob(server: tuple[str, Recorder]) -> None:
    url, _ = server
    client = client_for(url)
    under = find(client, path="/srv/media/series/Harbour Lights/Season 02")
    assert {row["Type"] for row in under} == {"Season", "Episode"}
    assert len(under) == 3
    globbed = find(client, path="*/harbour lights - s01e0[12].mkv")
    assert [row["IndexNumber"] for row in globbed] == [1, 2]
    with_backslashes = find(client, path="\\srv\\media\\series\\Harbour Lights\\Season 02")
    assert len(with_backslashes) == 3


def test_find_by_provider_id(server: tuple[str, Recorder]) -> None:
    url, _ = server
    wanted = TREE[3]["ProviderIds"]["Tvdb"]
    assert [r["Id"] for r in find(client_for(url), provider=f"tvdb={wanted}")] == [TREE[3]["Id"]]
    assert [r["Id"] for r in find(client_for(url), provider=wanted)] == [TREE[3]["Id"]]


def test_children_direct_recursive_and_extras(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    client = client_for(url)
    assert [r["Type"] for r in children(client, SERIES)] == ["Season", "Season"]
    assert [r["Id"] for r in children(client, SEASON_ONE)] == EPISODES[:3]
    everything = children(client, SERIES, recursive=True, types=["Episode"])
    assert [r["Id"] for r in everything] == EPISODES
    recorder.special_features[SERIES] = [{"Id": "00000000-0000-0000-0000-000000000900",
                                          "Name": "Behind the Harbour", "Type": "Video",
                                          "ExtraType": "BehindTheScenes"}]
    assert [r["Name"] for r in children(client, SERIES, extras=True)] == ["Behind the Harbour"]


def test_playstate_reads_every_user_as_that_user(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, EPISODES[1])] = {
        "Played": True, "PlayCount": 2, "PlaybackPositionTicks": 0,
        "LastPlayedDate": "2020-01-02T03:04:05Z", "IsFavorite": False}
    report = playstate(client_for(url), EPISODES[:3], workers=3)
    assert report.ok
    assert len(report.rows) == 3 * 3
    assert [r["ItemId"] for r in report.rows[:3]] == [EPISODES[0]] * 3
    row = report.state(EPISODES[1], SECOND_USER_ID)
    assert row is not None and row["Played"] is True and row["PlayCount"] == 2
    assert row["UserName"] == "second-fixture-user"
    asked_as = {user for user, _params in recorder.queries}
    assert asked_as == {USER_ID, SECOND_USER_ID, THIRD_USER_ID}


def test_playstate_batches_identifiers(server: tuple[str, Recorder],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import jfkit.query

    monkeypatch.setattr(jfkit.query, "BATCH", 2)
    url, recorder = server
    report = playstate(client_for(url), EPISODES, user_ids=[USER_ID], workers=2)
    assert len(report.rows) == len(EPISODES)
    assert len(recorder.queries) == 3


def test_playstate_names_what_it_could_not_find(server: tuple[str, Recorder]) -> None:
    url, _ = server
    ghost = "00000000-0000-0000-0000-000000000999"
    report = playstate(client_for(url), [EPISODES[0], ghost], user_ids=[USER_ID])
    assert report.missing == (ghost,) and not report.ok


def test_playstate_keeps_one_failed_user_to_itself(server: tuple[str, Recorder]) -> None:
    url, _ = server
    stranger = "00000000-0000-0000-0000-000000000077"
    report = playstate(client_for(url), EPISODES[:2], user_ids=[USER_ID, stranger])
    assert len(report.rows) == 2
    assert [user for user, _b, _e in report.errors] == [stranger]


def test_rows_render_as_tsv_and_json() -> None:
    rows = [item_row(row) for row in TREE[:2]]
    tsv = render(rows, ITEM_COLUMNS, "tsv").splitlines()
    assert tsv[0].split("\t") == list(ITEM_COLUMNS)
    assert tsv[1].split("\t")[2] == "Harbour Lights"
    assert json.loads(render(rows, ITEM_COLUMNS, "json"))[1]["Type"] == "Season"


@pytest.fixture
def configured(server: tuple[str, Recorder], tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch) -> Path:
    url, _ = server
    config = tmp_path / "mediatoolkit.toml"
    config.write_text(
        f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
        f'user_id = "{USER_ID}"\n', encoding="utf-8")
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    return config


def test_the_verbs_print_rows_and_exit_on_the_answer(
    configured: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    def run(*argv: str) -> int:
        return jfkit_cli.main(["--config", str(configured), *argv])

    assert run("find", "--name", "Harbour Lights", "--exact", "--type", "Series") == 0
    assert SERIES in capsys.readouterr().out
    assert run("find", "--name", "Nowhere At All") == 1
    capsys.readouterr()
    assert run("find") == 2
    capsys.readouterr()
    assert run("children", SEASON_ONE, "--format", "json") == 0
    assert [r["Id"] for r in json.loads(capsys.readouterr().out)] == EPISODES[:3]
    assert run("playstate", SERIES, "--recursive", "--jobs", "2") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split("\t") == list(PLAYSTATE_COLUMNS)
    assert len(lines) == 1 + len(EPISODES) * 3
