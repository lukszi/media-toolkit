"""Watched state carried across a renumber, including the rows handed out by slot.

The scenario: a season of four episodes is renumbered. Every episode gets a
new identifier, and the file that used to be labelled episode 1 turns out to
be episode 3. After the renumber the server hands the *new* episode 1 the
state recorded for the episode-1 slot -- the old file's -- which nobody ever
watched under that title. A replay has to write the snapshot onto the new
identifiers and clear that inherited row; the last-played date it carries
cannot be cleared and has to be reported instead.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jfkit import cli as jfkit_cli
from jfkit.userdata import (
    Snapshot,
    UserState,
    actions,
    expectations,
    load_mapping,
    plan_replay,
    snapshot,
    verify,
)
from mkvkit.steps import apply, read_audit

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

BEFORE = series_tree("Northwind", {1: 4}, first=100)
AFTER = series_tree("Northwind", {1: 4}, first=200)
OLD = [row["Id"] for row in BEFORE if row["Type"] == "Episode"]
NEW = [row["Id"] for row in AFTER if row["Type"] == "Episode"]
SERIES_AFTER = AFTER[0]["Id"]
#: old episode 1 holds what is really episode 3, 2 is 1, 3 is 2, 4 stays 4
MAPPING = {OLD[0]: NEW[2], OLD[1]: NEW[0], OLD[2]: NEW[1], OLD[3]: NEW[3]}
DATE = "2021-02-03T04:05:06.0000000Z"


def _state(**fields: Any) -> dict[str, Any]:
    base = {"Played": False, "PlayCount": 0, "PlaybackPositionTicks": 0,
            "IsFavorite": False, "LastPlayedDate": None}
    base.update(fields)
    return base


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        running[1].items += [dict(row) for row in BEFORE]
        running[1].user_data.update({
            (USER_ID, OLD[0]): _state(Played=True, PlayCount=2, LastPlayedDate=DATE),
            (SECOND_USER_ID, OLD[1]): _state(PlaybackPositionTicks=1200 * 10_000_000),
            (THIRD_USER_ID, OLD[2]): _state(IsFavorite=True),
        })
        yield running


def _renumber(recorder: Recorder) -> None:
    """New identifiers for every episode, and the slot's state handed to new episode 1."""
    recorder.items = [row for row in recorder.items if row["Id"] not in
                      {r["Id"] for r in BEFORE}] + [dict(row) for row in AFTER]
    recorder.user_data[(USER_ID, NEW[0])] = _state(Played=True, PlayCount=2,
                                                   LastPlayedDate=DATE)


def test_a_snapshot_holds_every_users_row(server: tuple[str, Recorder],
                                          tmp_path: Path) -> None:
    url, _ = server
    taken = snapshot(client_for(url), OLD, workers=3)
    assert set(taken.users) == {USER_ID, SECOND_USER_ID, THIRD_USER_ID}
    first = taken.item(OLD[0])
    assert first is not None and first.episode == 1 and first.series == "Northwind"
    assert first.states[USER_ID] == UserState(played=True, play_count=2, last_played=DATE)
    again = Snapshot.load(taken.save(tmp_path / "snap.json"))
    assert again.items == taken.items and again.users == taken.users


def test_a_snapshot_with_a_hole_is_refused(server: tuple[str, Recorder]) -> None:
    url, _ = server
    with pytest.raises(LookupError):
        snapshot(client_for(url), [*OLD, "00000000-0000-0000-0000-000000000999"])


def test_expectations_follow_the_mapping_and_blank_the_rest() -> None:
    taken = Snapshot(users=(USER_ID,), items=())
    assert expectations(taken, {}, ["x"]) == {("x", USER_ID): UserState()}
    with pytest.raises(ValueError, match="both map onto"):
        expectations(taken, {"a": "n", "b": "n"})


def test_the_replay_writes_the_snapshot_and_clears_the_slot_ghost(
    server: tuple[str, Recorder],
) -> None:
    url, recorder = server
    taken = snapshot(client_for(url), OLD)
    _renumber(recorder)

    dry = client_for(url)
    plan = plan_replay(dry, taken, MAPPING, scope=NEW)
    summaries = "\n".join(step.summary for step in plan.steps)
    assert len(plan.steps) == 4, plan.render()
    assert f"clear {NEW[0]} for user {USER_ID}" in summaries
    assert any("slot" in note for note in plan.notes)
    assert any("last-played" in note for note in plan.notes)
    assert recorder.user_data_writes == []

    live = client_for(url, dry_run=False)
    report = apply(plan, actions(live))
    assert report.ok, str(report)
    assert recorder.user_data[(USER_ID, NEW[2])]["PlayCount"] == 2
    assert recorder.user_data[(USER_ID, NEW[2])]["LastPlayedDate"] == DATE
    assert recorder.user_data[(SECOND_USER_ID, NEW[0])]["PlaybackPositionTicks"] > 0
    assert recorder.user_data[(THIRD_USER_ID, NEW[1])]["IsFavorite"] is True
    ghost = recorder.user_data[(USER_ID, NEW[0])]
    assert ghost["Played"] is False and ghost["PlayCount"] == 0

    checked = verify(live, taken, MAPPING, scope=NEW)
    assert checked.ok, str(checked)
    assert [(m.item, m.user) for m in checked.dates_left] == [(NEW[0], USER_ID)]
    assert "cannot" in str(checked)


def test_state_outside_the_mapping_is_cleared_when_the_scope_covers_it(
    server: tuple[str, Recorder],
) -> None:
    url, recorder = server
    taken = snapshot(client_for(url), OLD[:1])
    _renumber(recorder)
    mapping = {OLD[0]: NEW[2]}
    narrow = plan_replay(client_for(url), taken, mapping)
    assert all(step.params["item"] == NEW[2] for step in narrow.steps)
    wide = plan_replay(client_for(url), taken, mapping, scope=NEW)
    assert {step.params["item"] for step in wide.steps} == {NEW[2], NEW[0]}


def test_nothing_to_do_is_an_empty_plan(server: tuple[str, Recorder]) -> None:
    url, _ = server
    taken = snapshot(client_for(url), OLD)
    assert plan_replay(client_for(url), taken, {}).steps == ()


def test_an_interrupted_replay_resumes_from_its_audit(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, recorder = server
    taken = snapshot(client_for(url), OLD)
    _renumber(recorder)
    live = client_for(url, dry_run=False)
    plan = plan_replay(live, taken, MAPPING, scope=NEW)
    audit = tmp_path / "audit.jsonl"
    recorder.user_data_refused = {NEW[1]}
    first = apply(plan, actions(live), audit=audit)
    assert not first.ok and first.failed
    recorder.user_data_refused = set()
    second = apply(plan, actions(live), audit=audit)
    assert second.ok, str(second)
    assert "skipped" in {r.state for r in second.results}
    assert verify(live, taken, MAPPING, scope=NEW).ok
    assert any(e["event"] == "failed" for e in read_audit(audit))


def test_a_mapping_reads_from_json_or_tsv(tmp_path: Path) -> None:
    as_json = tmp_path / "map.json"
    as_json.write_text(json.dumps({"a": "b"}), encoding="utf-8")
    as_tsv = tmp_path / "map.tsv"
    as_tsv.write_text("old\tnew\n# a comment\na\tb\n", encoding="utf-8")
    assert load_mapping(as_json) == load_mapping(as_tsv) == {"a": "b"}


def test_the_verb_snapshots_replays_and_verifies(
    server: tuple[str, Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    config = tmp_path / "mediatoolkit.toml"
    config.write_text(
        f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
        f'user_id = "{USER_ID}"\n', encoding="utf-8")
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)

    def run(*argv: str) -> int:
        return jfkit_cli.main(["--config", str(config), *argv])

    snap = tmp_path / "snap.json"
    assert run("userdata", "snapshot", "--parent", BEFORE[0]["Id"], "--out", str(snap)) == 0
    assert "3 row(s) with state" in capsys.readouterr().out
    _renumber(recorder)
    mapping = tmp_path / "map.json"
    mapping.write_text(json.dumps(MAPPING), encoding="utf-8")
    common = ["userdata", "replay", str(snap), "--map", str(mapping),
              "--scope-parent", SERIES_AFTER]
    plan = tmp_path / "plan.json"
    assert run(*common, "--plan-out", str(plan)) == 0
    assert "dry run" in capsys.readouterr().out and recorder.user_data_writes == []
    assert run(*common, "--apply") == 2
    capsys.readouterr()
    audit = tmp_path / "audit.jsonl"
    assert run(*common, "--plan", str(plan), "--apply", "--audit", str(audit)) == 0
    out = capsys.readouterr().out
    assert "4 done" in out and "0 differ" in out
    assert run("userdata", "verify", str(snap), "--map", str(mapping),
               "--scope-parent", SERIES_AFTER) == 0
    recorder.user_data[(THIRD_USER_ID, NEW[3])] = _state(Played=True, PlayCount=1)
    assert run("userdata", "verify", str(snap), "--map", str(mapping),
               "--scope-parent", SERIES_AFTER) == 1
