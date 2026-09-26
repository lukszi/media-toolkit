"""``jfkit rename``: one audited plan from a mapping to a verified end state.

The library here is real files in a temporary folder and a stand-in server
whose catalogue follows them: a path notification runs :meth:`Library.scan`,
which drops the rows whose file is gone and adds a row -- with a new
identifier -- for every video that has none, reading its numbers with the
naming port the way the server reads them. A new episode that lands on a
season/episode slot an old one held is handed that slot's watched state, as
the server does (gotcha 5.6), so the replay has ghosts to clear.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import itertools
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jfkit import cli as jfkit_cli
from jfkit.naming import parse
from jfkit.rename import Options, Pair, infer_expect, parse_expect, plan_files, read_mapping
from jfkit.rename import verb as rename_verb
from mkvkit.steps import load_plan, read_audit

from tests.fake_server import (
    CREDENTIAL_VARIABLE,
    FIXTURE_CREDENTIAL,
    SECOND_USER_ID,
    USER_ID,
    Recorder,
    fake_server,
)

DATE = "2021-02-03T04:05:06.0000000Z"
VIDEO = (".mkv", ".mp4")


def ident(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def unplayed() -> dict[str, Any]:
    return {"Played": False, "PlayCount": 0, "PlaybackPositionTicks": 0,
            "IsFavorite": False}


def state(**fields: Any) -> dict[str, Any]:
    return {**unplayed(), "LastPlayedDate": None, **fields}


class Library:
    """A series library on disk, and the stand-in's catalogue kept in step with it."""

    def __init__(self, root: Path, recorder: Recorder) -> None:
        self.root = root
        self.recorder = recorder
        self.numbers = itertools.count(1000)
        self.series: dict[str, str] = {}
        #: (series, season, episode) -> user -> the state recorded for that slot
        self.slots: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        self.scanning = True
        recorder.items = []
        recorder.virtual_folders = [
            {"Name": "Shows", "CollectionType": "tvshows", "Locations": [str(root)]},
        ]
        recorder.on_notify = lambda _updates: self.scan() if self.scanning else None

    def add(self, name: str, files: Mapping[str, str]) -> Path:
        """A series folder with these files (a name ending in / is a folder)."""
        folder = self.root / name
        for relative, content in files.items():
            path = folder / relative
            if relative.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        series_id = ident(next(self.numbers))
        self.recorder.items.append({
            "Id": series_id, "Name": name, "Type": "Series", "ParentId": None,
            "Path": str(folder), "UserData": unplayed(),
        })
        self.series[str(folder)] = series_id
        self.scan()
        return folder

    def _season(self, series_id: str, folder: str, number: int) -> str:
        for row in self.recorder.items:
            if (row["Type"] == "Season" and row.get("SeriesId") == series_id
                    and row.get("IndexNumber") == number):
                return str(row["Id"])
        season_id = ident(next(self.numbers))
        self.recorder.items.append({
            "Id": season_id, "Name": f"Season {number}", "Type": "Season",
            "ParentId": series_id, "SeriesId": series_id, "IndexNumber": number,
            "Path": f"{folder}{os.sep}Season {number:02d}", "UserData": unplayed(),
        })
        return season_id

    def scan(self) -> None:
        recorder = self.recorder
        for row in list(recorder.items):
            if row["Type"] not in ("Episode", "Video") or Path(row["Path"]).exists():
                continue
            recorder.items.remove(row)
            for extras in recorder.special_features.values():
                if row in extras:
                    extras.remove(row)
            if row["Type"] == "Episode":
                slot = (row["SeriesId"], row["ParentIndexNumber"], row["IndexNumber"])
                for (user, item), data in recorder.user_data.items():
                    if item == row["Id"]:
                        self.slots.setdefault(slot, {})[user] = dict(data)
        known = {str(row.get("Path")).casefold() for row in recorder.items}
        for folder, series_id in self.series.items():
            for path in sorted(Path(folder).rglob("*")):
                if (path.suffix.lower() not in VIDEO or not path.is_file()
                        or str(path).casefold() in known):
                    continue
                found = parse(path)
                if found.is_extra:
                    row = {"Id": ident(next(self.numbers)), "Name": path.stem,
                           "Type": "Video", "ExtraType": found.extra, "ParentId": None,
                           "Path": str(path), "UserData": unplayed()}
                    recorder.items.append(row)
                    recorder.special_features.setdefault(series_id, []).append(row)
                    continue
                season = found.effective_season if found.effective_season is not None else 1
                season_id = self._season(series_id, folder, season)
                row = {
                    "Id": ident(next(self.numbers)), "Name": path.stem, "Type": "Episode",
                    "ParentId": season_id, "SeasonId": season_id, "SeriesId": series_id,
                    "SeriesName": Path(folder).name, "ParentIndexNumber": season,
                    "IndexNumber": found.episode, "IndexNumberEnd": found.end,
                    "Path": str(path), "UserData": unplayed(),
                }
                recorder.items.append(row)
                for user, data in self.slots.get((series_id, season, found.episode), {}).items():
                    recorder.user_data[(user, str(row["Id"]))] = dict(data)

    def at(self, path: Path) -> dict[str, Any]:
        for row in self.recorder.items:
            if str(row.get("Path")).casefold() == str(path).casefold():
                return row
        raise KeyError(path)

    def user_state(self, user: str, path: Path) -> dict[str, Any]:
        row = self.at(path)
        return self.recorder.user_data.get((user, str(row["Id"]))) or dict(row["UserData"])


@dataclass
class Env:
    config: Path
    library: Library
    recorder: Recorder
    tmp: Path
    park: Path

    def run(self, *argv: str) -> int:
        return jfkit_cli.main(["--config", str(self.config), "rename", *argv])

    def mapping(self, rows: list[tuple[Path, Path] | tuple[Path, Path, str]]) -> Path:
        path = self.tmp / "mapping.tsv"
        lines = ["old\tnew\texpect"] + ["\t".join(str(c) for c in row) for row in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    with fake_server() as (url, recorder):
        root = tmp_path / "lib"
        root.mkdir()
        park = tmp_path / "parked"
        config = tmp_path / "mediatoolkit.toml"
        config.write_text(
            "[server]\n"
            f'url = "{url}"\n'
            f'token_env = "{CREDENTIAL_VARIABLE}"\n'
            f'user_id = "{USER_ID}"\n'
            "\n[paths]\n"
            f'series = "{root.as_posix()}"\n'
            f'parked = "{park.as_posix()}"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
        monkeypatch.setattr(rename_verb, "_sleep", lambda _s: None)
        yield Env(config, Library(root, recorder), recorder, tmp_path, park)


def _northwind(env: Env) -> Path:
    """Season 1 of four episodes; the first two carry sidecars."""
    return env.library.add("Northwind", {
        "Season 01/Northwind - S01E01.mkv": "one",
        "Season 01/Northwind - S01E01.nfo": "<episodedetails>one</episodedetails>",
        "Season 01/Northwind - S01E01-thumb.jpg": "thumb one",
        "Season 01/Northwind - S01E01.en.srt": "subtitle one",
        "Season 01/Northwind - S01E01.trickplay/320 - 10x10/0.jpg": "tiles one",
        "Season 01/Northwind - S01E02.mkv": "two",
        "Season 01/Northwind - S01E02.nfo": "<episodedetails>two</episodedetails>",
        "Season 01/Northwind - S01E03.mkv": "three",
        "Season 01/Northwind - S01E04.mkv": "four",
    })


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _posts(recorder: Recorder) -> list[tuple[str, str]]:
    return [r for r in recorder.requests if r[0] != "GET"]


# ------------------------------------------------------------ the whole run
def test_a_renumber_with_a_cycle_and_a_chain_carries_files_and_state(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    e = {n: season / f"Northwind - S01E{n:02d}.mkv" for n in range(1, 6)}
    recorder = env.recorder
    recorder.user_data[(USER_ID, str(env.library.at(e[1])["Id"]))] = state(
        Played=True, PlayCount=2, LastPlayedDate=DATE)
    recorder.user_data[(SECOND_USER_ID, str(env.library.at(e[2])["Id"]))] = state(
        PlaybackPositionTicks=1200 * 10_000_000)
    recorder.user_data[(USER_ID, str(env.library.at(e[4])["Id"]))] = state(IsFavorite=True)
    # 1 and 2 swap places; 3 moves to the free slot 5 and 4 stays
    mapping = env.mapping([(e[1], e[2]), (e[2], e[1]), (e[3], e[5])])
    work = env.tmp / "work"

    assert env.run("--map", str(mapping), "--apply", "--work", str(work), "--poll", "0") == 0

    assert (_read(e[1]), _read(e[2]), _read(e[5]), _read(e[4])) == (
        "two", "one", "three", "four")
    assert not e[3].exists()
    assert _read(season / "Northwind - S01E02-thumb.jpg") == "thumb one"
    assert _read(season / "Northwind - S01E02.en.srt") == "subtitle one"
    assert _read(season / "Northwind - S01E02.trickplay/320 - 10x10/0.jpg") == "tiles one"
    assert not list(season.glob("*.rename-*"))
    # both documents were written for other numbers: parked, not carried
    assert not list(season.glob("*.nfo"))
    parked = sorted(p.name for p in env.park.rglob("*.nfo"))
    assert parked == ["0001-Northwind - S01E01.nfo", "0002-Northwind - S01E02.nfo"]

    steps = [s.id.split(":")[0] for s in load_plan(work / "plan.json").steps]
    assert {"park", "stage", "rename", "settle", "notify"} <= set(steps)
    # the old E01 (content "one", now E02) keeps its watcher; the new E01 does not
    # inherit the slot's state, and the untouched E04 keeps its favourite
    assert env.library.user_state(USER_ID, e[2])["PlayCount"] == 2
    assert env.library.user_state(USER_ID, e[1])["Played"] is False
    assert env.library.user_state(SECOND_USER_ID, e[1])["PlaybackPositionTicks"] == \
        1200 * 10_000_000
    assert env.library.user_state(SECOND_USER_ID, e[2])["PlaybackPositionTicks"] == 0
    assert env.library.user_state(USER_ID, e[4])["IsFavorite"] is True
    ids = json.loads(_read(work / "ids.json"))
    assert len(ids) == 3
    events = [entry["event"] for entry in read_audit(work / "audit.jsonl")]
    assert {"rename.wait", "rename.replay", "rename.verify"} <= set(events)
    verified = [x for x in read_audit(work / "audit.jsonl") if x["event"] == "rename.verify"]
    assert verified[-1]["ok"] is True
    assert "as intended" in _read(work / "report.txt")


def test_the_notification_names_only_the_deepest_changed_folder(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    old, new = season / "Northwind - S01E03.mkv", season / "Northwind - S01E05.mkv"
    assert env.run(str(old), str(new), "--apply", "--work", str(env.tmp / "w"),
                   "--poll", "0") == 0
    assert [u["Path"] for u in env.recorder.notifications] == [str(season)]


def test_a_move_between_seasons_notifies_both_folders_in_batches(
    env: Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jfkit.rename import server as rename_server

    monkeypatch.setattr(rename_server, "NOTIFY_BATCH", 1)
    series = _northwind(env)
    old = series / "Season 01" / "Northwind - S01E04.mkv"
    new = series / "Season 02" / "Northwind - S02E01.mkv"
    work = env.tmp / "w"
    assert env.run(str(old), str(new), "--apply", "--work", str(work), "--poll", "0",
                   "--refresh", "items") == 0
    notify = [s for s in load_plan(work / "plan.json").steps if s.action == "rename.notify"]
    assert len(notify) == 2
    assert {u["Path"] for u in env.recorder.notifications} == {
        str(series / "Season 01"), str(series / "Season 02")}
    item = env.library.at(new)
    assert (item["ParentIndexNumber"], item["IndexNumber"]) == (2, 1)
    assert env.recorder.refreshed == [str(item["Id"])]


def test_nothing_starts_while_somebody_is_watching(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    env.recorder.sessions = [{"NowPlayingItem": {"Id": ident(1)},
                              "PlayState": {"IsPaused": False}}]
    old = season / "Northwind - S01E03.mkv"
    assert env.run(str(old), str(season / "Northwind - S01E05.mkv"), "--apply",
                   "--work", str(env.tmp / "w"), "--poll", "0") == 1
    assert "somebody is watching" in capsys.readouterr().out
    assert old.exists() and _posts(env.recorder) == []


def test_the_dry_run_changes_nothing_and_sends_nothing(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    before = sorted(p.name for p in season.iterdir())
    old, new = season / "Northwind - S01E03.mkv", season / "Northwind - S01E05.mkv"
    assert env.run(str(old), str(new)) == 0
    out = capsys.readouterr().out
    assert "nothing was changed" in out and "notify" in out
    assert sorted(p.name for p in season.iterdir()) == before
    assert _posts(env.recorder) == []
    assert not env.park.exists()


def test_apply_needs_a_work_folder(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    assert env.run(str(season / "Northwind - S01E03.mkv"),
                   str(season / "Northwind - S01E05.mkv"), "--apply") == 2
    assert (season / "Northwind - S01E03.mkv").exists()


# ------------------------------------------------------------ refusals
def test_collisions_are_refused_and_nothing_moves(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    e = {n: season / f"Northwind - S01E{n:02d}.mkv" for n in range(1, 5)}
    mapping = env.mapping([
        (e[1], e[4]),                                    # E04 stays: occupied
        (e[2], season / "Northwind - S01E07.mkv"),
        (e[3], season / "Northwind - S01E07.mkv"),       # two onto one target
    ])
    assert env.run("--map", str(mapping), "--apply", "--work", str(env.tmp / "w")) == 1
    out = capsys.readouterr().out
    assert "a rename never replaces anything" in out
    assert "two renames end here" in out
    assert all(p.exists() for p in e.values())
    assert _posts(env.recorder) == []


def test_one_old_path_in_two_lines_is_refused(tmp_path: Path) -> None:
    season = tmp_path / "Northwind" / "Season 01"
    season.mkdir(parents=True)
    old = season / "Northwind - S01E03.mkv"
    old.write_text("three", encoding="utf-8")
    files = plan_files([Pair(old, season / "Northwind - S01E05.mkv"),
                        Pair(old, season / "Northwind - S01E06.mkv")])
    assert any("a path can go one place only" in p.reason for p in files.problems)


def test_two_files_in_one_slot_are_refused(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    files = plan_files(
        [Pair(season / "Northwind - S01E03.mkv", season / "Northwind - S01E04 - Winter Tide.mkv")],
        Options(roots={str(env.library.root): "tvshows"}),
    )
    assert any("S01E04 would be held by 2 files" in p.reason and "merges" in p.reason
               for p in files.problems)


def test_a_file_the_new_name_would_adopt_is_refused(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    (season / "Northwind - S01E05.nfo").write_text("left behind", encoding="utf-8")
    files = plan_files([Pair(season / "Northwind - S01E03.mkv",
                             season / "Northwind - S01E05.mkv")])
    assert any("would be read as belonging to it" in p.reason for p in files.problems)


def test_a_path_too_long_with_its_preview_tiles_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    old = season / "Northwind - S01E03.mkv"
    title = " ".join(["Golden Meridian"] * 16)
    new = season / f"Northwind - S01E03 - {title}.mkv"
    assert env.run(str(old), str(new)) == 1
    assert "preview tiles" in capsys.readouterr().out
    assert old.exists()


def test_a_name_read_as_something_else_is_refused_with_the_reason(tmp_path: Path) -> None:
    folder = tmp_path / "Signal Hill"
    folder.mkdir()
    old = folder / "Signal Hill-8000 Golden Quarry (1_2).mp4"
    old.write_text("x", encoding="utf-8")
    assert parse(old).season == 80
    dotted = plan_files([Pair(old, folder / "Signal Hill-8.000 Golden Quarry (1_2).mp4")])
    assert any("no number was intended" in p.reason for p in dotted.problems)
    words = plan_files([Pair(old, folder / "Signal Hill-Eight Thousand Golden Quarry (1_2).mp4")])
    assert words.ok, words.problems
    assert words.videos[0].changed


def test_an_intention_given_explicitly_is_what_is_checked(tmp_path: Path) -> None:
    season = tmp_path / "Northwind" / "Season 02"
    season.mkdir(parents=True)
    old = season / "E16.Golden Anchor (1).mkv"
    old.write_text("x", encoding="utf-8")
    new = season / "E17.Golden Anchor (2).mkv"
    assert not plan_files([Pair(old, new)]).ok      # inferred: no number intended
    assert plan_files([Pair(old, new, parse_expect("S02E17"))]).ok
    wrong = plan_files([Pair(old, new, parse_expect("S02E18"))])
    assert any("read as episode 17, not 18" in p.reason for p in wrong.problems)


def test_a_library_root_is_not_notified(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    folder = _northwind(env)
    renamed = folder.with_name("Northwind (1978)")
    assert env.run(str(folder), str(renamed)) == 1
    assert "validation of the whole library" in capsys.readouterr().out
    assert env.run(str(folder), str(renamed), "--allow-library-scan") == 0
    assert folder.is_dir() and _posts(env.recorder) == []


def test_a_new_folder_the_server_does_not_know_is_refused(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    target = env.library.root / "Winter Ledger" / "Season 01" / "Winter Ledger - S01E01.mkv"
    assert env.run(str(season / "Northwind - S01E04.mkv"), str(target)) == 1
    assert "is new to the server" in capsys.readouterr().out


def test_the_library_root_itself_is_never_renamed(env: Env) -> None:
    files = plan_files([Pair(env.library.root, env.library.root.with_name("elsewhere"))],
                       Options(roots={str(env.library.root): "tvshows"}))
    assert any("a root is never renamed" in p.reason for p in files.problems)


# ------------------------------------------------------------ sidecars and parking
def test_sidecars_follow_and_only_a_stale_document_is_parked(tmp_path: Path) -> None:
    season = tmp_path / "Northwind" / "Season 01"
    season.mkdir(parents=True)
    for name in ("Northwind - S01E03.mkv", "Northwind - S01E03.nfo",
                 "Northwind - S01E03-thumb.jpg", "Northwind - S01E03.txt"):
        (season / name).write_text(name, encoding="utf-8")
    park = tmp_path / "park"
    old = season / "Northwind - S01E03.mkv"
    retitled = plan_files([Pair(old, season / "Northwind - S01E03 - Quiet Harbour.mkv")],
                          Options(park=park))
    assert retitled.ok and not retitled.parked          # same numbers: carried
    assert len(retitled.moves) == 4
    renumbered = plan_files([Pair(old, season / "Northwind - S01E09.mkv")], Options(park=park))
    assert renumbered.ok and len(renumbered.parked) == 1
    assert [m[1].name for m in renumbered.moves] == [
        "Northwind - S01E09.mkv", "Northwind - S01E09-thumb.jpg", "Northwind - S01E09.txt"]
    unparked = plan_files([Pair(old, season / "Northwind - S01E09.mkv")])
    assert any("stale .nfo" in p.reason for p in unparked.problems)
    inside = plan_files([Pair(old, season / "Northwind - S01E09.mkv")],
                        Options(park=season / "parked"))
    assert any("inside a library" in p.reason for p in inside.problems)


def test_files_only_renames_without_a_server(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    season = tmp_path / "Northwind" / "Season 01"
    season.mkdir(parents=True)
    (season / "Northwind - S01E03.mkv").write_text("three", encoding="utf-8")
    (season / "Northwind - S01E03.nfo").write_text("doc", encoding="utf-8")
    config = tmp_path / "mediatoolkit.toml"
    config.write_text("", encoding="utf-8")
    argv = ["--config", str(config), "rename", str(season / "Northwind - S01E03.mkv"),
            str(season / "Northwind - S01E08.mkv"), "--park", str(tmp_path / "park")]
    assert jfkit_cli.main(argv) == 2                   # no server: says so
    assert jfkit_cli.main([*argv, "--files-only", "--apply", "--work",
                           str(tmp_path / "w")]) == 0
    assert _read(season / "Northwind - S01E08.mkv") == "three"
    assert list((tmp_path / "park").rglob("*.nfo"))


# ------------------------------------------------------------ extras
def test_an_extras_folder_rename_turns_episodes_into_extras(env: Env) -> None:
    folder = env.library.add("Harbour Lights", {
        "Season 07/Harbour Lights - S07E01.mkv": "episode",
        "Season 07/Feaaturettes/Winter Foundry.mkv": "featurette",
        "Season 07/Feaaturettes/Winter Foundry.nfo": "<episodedetails/>",
        "Season 07/Feaaturettes/Winter Foundry.trickplay/320 - 10x10/0.jpg": "tiles",
    })
    wrong = folder / "Season 07" / "Feaaturettes"
    assert env.library.at(wrong / "Winter Foundry.mkv")["Type"] == "Episode"
    right = wrong.with_name("Featurettes")
    work = env.tmp / "w"
    assert env.run(str(wrong), str(right), "--apply", "--work", str(work), "--poll", "0") == 0
    extra = env.library.at(right / "Winter Foundry.mkv")
    assert extra["ExtraType"] == "Featurette"
    assert (right / "Winter Foundry.trickplay" / "320 - 10x10" / "0.jpg").is_file()
    assert not (right / "Winter Foundry.nfo").exists()
    assert list(env.park.rglob("*Winter Foundry.nfo"))
    assert "as intended" in _read(work / "report.txt")


# ------------------------------------------------------------ resume
def test_a_run_stopped_by_a_held_file_resumes_where_it_stopped(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    e = {n: season / f"Northwind - S01E{n:02d}.mkv" for n in range(1, 6)}
    mapping = env.mapping([(e[1], e[2]), (e[2], e[1]), (e[3], e[5])])
    work = env.tmp / "w"
    real_unlink = os.unlink

    def held(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == e[3]:
            raise PermissionError(13, "Access is denied", str(path))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", held)
    assert env.run("--map", str(mapping), "--apply", "--work", str(work), "--poll", "0",
                   "--attempts", "2") == 1
    assert "to resume" in capsys.readouterr().out
    assert e[3].exists() and not e[5].exists()
    assert list(season.glob("*.rename-*"))                 # the cycle is half way
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert env.run("--apply", "--work", str(work), "--poll", "0") == 0
    assert (_read(e[1]), _read(e[2]), _read(e[5])) == ("two", "one", "three")
    assert not list(season.glob("*.rename-*"))
    skipped = [x for x in read_audit(work / "audit.jsonl") if x["event"] == "skipped"]
    assert skipped


def test_a_briefly_held_file_is_retried(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    season = _northwind(env) / "Season 01"
    old = season / "Northwind - S01E03.mkv"
    real_unlink = os.unlink
    refusals = [1]

    def once(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == old and refusals:
            refusals.pop()
            raise PermissionError(13, "Access is denied", str(path))
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", once)
    assert env.run(str(old), str(season / "Northwind - S01E05.mkv"), "--apply",
                   "--work", str(env.tmp / "w"), "--poll", "0") == 0
    assert not refusals and _read(season / "Northwind - S01E05.mkv") == "three"


def test_a_work_folder_belongs_to_one_mapping(env: Env) -> None:
    season = _northwind(env) / "Season 01"
    work = env.tmp / "w"
    env.library.scanning = False
    assert env.run(str(season / "Northwind - S01E03.mkv"), str(season / "Northwind - S01E05.mkv"),
                   "--apply", "--work", str(work), "--poll", "0", "--wait-timeout", "0") == 1
    assert env.run(str(season / "Northwind - S01E04.mkv"), str(season / "Northwind - S01E06.mkv"),
                   "--apply", "--work", str(work)) == 2


def test_a_server_that_has_not_caught_up_is_waited_for_again(
    env: Env, capsys: pytest.CaptureFixture[str],
) -> None:
    season = _northwind(env) / "Season 01"
    work = env.tmp / "w"
    env.library.scanning = False
    args = ("--apply", "--work", str(work), "--poll", "0", "--wait-timeout", "0")
    assert env.run(str(season / "Northwind - S01E03.mkv"),
                   str(season / "Northwind - S01E05.mkv"), *args) == 1
    assert "NOT YET AN ITEM" in capsys.readouterr().out
    env.library.scan()
    assert env.run(*args) == 0
    assert env.library.at(season / "Northwind - S01E05.mkv")["IndexNumber"] == 5


def test_a_slot_ghost_is_cleared_and_its_date_reported(env: Env,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    season = _northwind(env) / "Season 01"
    e3, e5 = season / "Northwind - S01E03.mkv", season / "Northwind - S01E05.mkv"
    # somebody watched slot 5 once, under a file that is long gone
    series_id = env.library.series[str(season.parent)]
    env.library.slots[(series_id, 1, 5)] = {
        USER_ID: state(Played=True, PlayCount=1, LastPlayedDate=DATE)}
    work = env.tmp / "w"
    assert env.run(str(e3), str(e5), "--apply", "--work", str(work), "--poll", "0") == 0
    out = capsys.readouterr().out
    assert "clear" in out
    assert env.library.user_state(USER_ID, e5)["Played"] is False
    assert "keep a last-played date" in out


# ------------------------------------------------------------ the pieces
def test_a_mapping_is_read_with_its_intentions() -> None:
    text = ("old\tnew\texpect\n# a comment\n\n"
            "/srv/media/series/a.mkv\t/srv/media/series/b.mkv\n"
            "/srv/media/series/c.mkv\t/srv/media/series/d.mkv\tS01E03-E04\n"
            "/srv/media/series/e.mkv\t/srv/media/series/f.mkv\textra:Featurette\n")
    pairs = read_mapping(text)
    assert [p.line for p in pairs] == [4, 5, 6]
    assert pairs[0].expect is None
    assert str(pairs[1].expect) == "S01E03-E04"
    assert pairs[2].expect is not None and pairs[2].expect.extra == "Featurette"
    with pytest.raises(ValueError, match="line 1"):
        read_mapping("only-one-column\n")
    with pytest.raises(ValueError, match="not an intention"):
        read_mapping("a.mkv\tb.mkv\tsometimes\n")


def test_intentions_are_inferred_from_the_new_name() -> None:
    assert str(infer_expect("/srv/media/series/Northwind/Northwind - S01E03.mkv")) == "S01E03"
    assert infer_expect("/srv/media/series/Northwind/Extras/Blue Aviary.mkv").kind == "extra"
    assert infer_expect("/srv/media/movies/Blue Canyon (1998).mkv", "movies").kind == "movie"
    assert infer_expect("/srv/media/series/Northwind/Blue Aviary.mkv").kind == "none"
    assert parse_expect("auto") is None and parse_expect("E03") is not None
