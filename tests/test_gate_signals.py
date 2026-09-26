"""The gate's signals: everything that can be using a disk, and which one held it.

The gate once said "clear" while the server was generating preview tiles on
that very disk. Its decoder ran as another account, so its command line came
back empty; the work had been started by a change the server noticed, not by
a scheduled task; and a user's playback from the same disk was not looked at
at all. Each test here is one of those, given to the gate as an input -- no
test reads the machine's processes, its disks or a real server.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jfkit import cli as jfkit_cli
from jfkit.diskactivity import MIB, Activity, BusyLimits, Counters, between, parse_diskstats
from jfkit.diskactivity import sample as sample_activity
from jfkit.jobs import (
    Gate,
    GateTimeout,
    LaneGate,
    Process,
    ServerView,
    Signal,
    gate,
    parse_process_lines,
    server_view,
    wait_until_clear,
)

from tests.fake_server import CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL, USER_ID, client_for
from tests.fake_server import fake_server as running_server

DEVICE = "/srv"
OTHER = "/mnt/archive"

QUIET = Activity(device=DEVICE, window_s=1.0, read_bps=0.0, write_bps=0.0, busy=0.01)
BUSY = Activity(device=DEVICE, window_s=1.0, read_bps=120 * MIB, write_bps=0.0, busy=0.97)

SERVER = Process(pid=40, name="jellyfin.exe", command="")
#: the server's decoder: another account's, so its command line is unreadable
HIDDEN = Process(pid=41, name="ffmpeg.exe", command="", parent=40, owner=None)


def playing(path: str, *, paused: bool = False, transcoding: bool = False) -> dict[str, Any]:
    session: dict[str, Any] = {
        "UserName": "first-fixture-user",
        "NowPlayingItem": {"Name": "Northwind S01E03", "Path": path},
        "PlayState": {"IsPaused": paused},
    }
    if transcoding:
        session["TranscodingInfo"] = {"VideoCodec": "h264"}
    return session


# ------------------------------------------------------------ hidden readers
def test_a_decoder_whose_command_line_cannot_be_read_holds_the_gate() -> None:
    held = gate(DEVICE, processes=[SERVER, HIDDEN])
    assert not held.open
    assert held.red_by == ("hidden-reader",)
    assert "started by jellyfin.exe (40)" in str(held)
    assert "no disk counters to rule it out" in str(held)


def test_a_hidden_decoder_is_a_note_when_this_disk_was_quiet() -> None:
    """It is reading something, and the disk's own counters say not this."""
    clear = gate(DEVICE, processes=[SERVER, HIDDEN], activity=QUIET)
    assert clear.open
    assert [s.kind for s in clear.notes] == ["hidden-reader"]
    assert "note: [hidden-reader]" in str(clear)


def test_a_hidden_decoder_on_a_busy_disk_holds_it_twice_over() -> None:
    held = gate(DEVICE, processes=[SERVER, HIDDEN], activity=BUSY)
    assert held.red_by == ("hidden-reader", "disk-activity")


def test_a_readable_reader_elsewhere_is_not_a_hidden_one() -> None:
    elsewhere = Process(pid=7, name="ffmpeg", command=f"ffmpeg -i {OTHER}/a.mkv")
    assert gate(DEVICE, processes=[elsewhere]).open


def test_the_listing_query_output_is_read_with_parent_and_owner() -> None:
    lines = "\n".join([
        "40|1||jellyfin.exe|",
        "41|40|SERVICE\\account|ffmpeg.exe|",
        "42|12||ffmpeg.exe|ffmpeg -i /srv/media/a.mkv -f null -",
        "garbage",
    ])
    found = {p.pid: p for p in parse_process_lines(lines)}
    assert found[41].parent == 40 and found[41].owner == "SERVICE\\account"
    assert found[41].command_hidden and found[41].is_heavy_reader
    assert found[42].command.endswith("-f null -") and not found[42].command_hidden
    assert set(found) == {40, 41, 42}


# ------------------------------------------------------------------ playback
def test_playback_from_this_disk_holds_it_and_says_who() -> None:
    held = gate(DEVICE, processes=[], sessions=[
        playing("/srv/media/series/Northwind/Season 1/Northwind - S01E03.mkv",
                transcoding=True)
    ])
    assert held.red_by == ("playback",)
    assert "first-fixture-user is playing Northwind S01E03" in str(held)
    assert "transcoding" in str(held)


def test_playback_elsewhere_or_paused_does_not_hold_it() -> None:
    assert gate(DEVICE, processes=[], sessions=[
        playing(f"{OTHER}/Northwind - S01E03.mkv")
    ]).open
    paused = gate(DEVICE, processes=[], sessions=[
        playing("/srv/media/Northwind - S01E03.mkv", paused=True)
    ])
    assert paused.open and paused.notes[0].kind == "playback"
    assert gate(DEVICE, processes=[], sessions=[{"UserName": "idle"}]).open


# ---------------------------------------------------- tasks, changes, locks
def test_a_running_task_holds_every_disk() -> None:
    held = gate(DEVICE, processes=[], running_tasks=["Scan Media Library"])
    assert held.red_by == ("task",)
    assert "reads everything it can" in str(held)


def test_recent_changes_on_this_disk_hold_it_unless_it_is_quiet() -> None:
    recent = [
        {"Name": "Harbour Lights S01E01", "Path": "/srv/media/series/Harbour Lights/1.mkv"},
        {"Name": "Blue Canyon", "Path": f"{OTHER}/Blue Canyon (1998).mkv"},
    ]
    held = gate(DEVICE, processes=[], recent=recent, recent_window_s=600)
    assert held.red_by == ("recent-changes",)
    assert "1 item(s) on this device changed in the last 10 min" in str(held)
    assert gate(DEVICE, processes=[], recent=recent, activity=QUIET).open
    assert gate(OTHER, processes=[], recent=recent[:1]).open


def test_a_lock_file_holds_the_gate_and_is_quoted(tmp_path: Path) -> None:
    lock = tmp_path / "LIVE-DISK.lock"
    assert gate(DEVICE, processes=[], locks=[lock]).open
    lock.write_text("feat/other 2026-01-01T00:00:00Z\n", encoding="utf-8")
    held = gate(DEVICE, processes=[], locks=[lock])
    assert held.red_by == ("lock",)
    assert "feat/other" in str(held)


def test_a_server_that_cannot_be_asked_is_not_taken_as_idle() -> None:
    held = gate(DEVICE, processes=[], server_error="connection refused")
    assert held.red_by == ("server",)


def test_the_report_names_every_signal_that_held_it_in_a_fixed_order(tmp_path: Path) -> None:
    lock = tmp_path / "held.lock"
    lock.write_text("x", encoding="utf-8")
    held = gate(
        DEVICE, processes=[SERVER, HIDDEN], running_tasks=["Extract chapter images"],
        sessions=[playing("/srv/media/Northwind.mkv")], activity=BUSY, locks=[lock],
    )
    assert held.red_by == ("hidden-reader", "playback", "task", "disk-activity", "lock")
    assert str(held).startswith(f"{DEVICE}: held, RED by hidden-reader, playback, task")
    document = held.as_dict()
    assert document["open"] is False and document["red_by"][0] == "hidden-reader"
    json.dumps(document)


def test_a_gate_built_by_hand_keeps_working() -> None:
    """Callers that build a Gate from reasons alone still get an answer."""
    held = Gate(device=DEVICE, reasons=("something",))
    assert not held.open and "held" in str(held)
    assert Signal("lock", "x").holds


# ------------------------------------------------------------- disk counters
def test_two_readings_are_turned_into_rates() -> None:
    first = Counters(read_bytes=0, written_bytes=0, busy_s=10.0, at_s=100.0, queue=0)
    second = Counters(read_bytes=200 * MIB, written_bytes=MIB, busy_s=11.8, at_s=102.0,
                      queue=3)
    found = between(DEVICE, first, second)
    assert found.read_bps == pytest.approx(100 * MIB)
    assert found.busy == pytest.approx(0.9)
    assert "read 100.0 MiB/s" in str(found) and "3 request(s) queued" in str(found)
    assert BusyLimits().reason(found) is not None
    assert BusyLimits().quiet(QUIET)


def test_a_sample_is_two_readings_a_window_apart() -> None:
    readings = iter([
        Counters(0, 0, 0.0, 0.0), Counters(MIB, 0, 0.1, 1.0),
    ])
    slept: list[float] = []
    found = sample_activity(DEVICE, window_s=1.0, read=lambda _d: next(readings),
                            sleep=slept.append)
    assert slept == [1.0] and found is not None and found.read_bps == MIB
    assert sample_activity(DEVICE, read=lambda _d: None, sleep=slept.append) is None


def test_the_kernel_table_is_read_for_the_named_device() -> None:
    table = (
        "   8       0 sda 100 0 2000 50 10 0 400 5 0 900 55 0 0 0 0\n"
        "   8       1 sda1 90 0 1800 45 9 0 380 4 2 880 50 0 0 0 0\n"
    )
    found = parse_diskstats(table, "sda1", at_s=5.0)
    assert found is not None
    assert found.read_bytes == 1800 * 512 and found.written_bytes == 380 * 512
    assert found.busy_s == pytest.approx(0.88) and found.queue == 2
    assert parse_diskstats(table, "sdb", at_s=5.0) is None


# --------------------------------------------------------- waiting and lanes
def test_waiting_looks_again_until_clear() -> None:
    answers = iter([
        Gate(DEVICE, reasons=("busy",)), Gate(DEVICE, reasons=("busy",)), Gate(DEVICE),
    ])
    slept: list[float] = []
    found = wait_until_clear(lambda: next(answers), poll_s=30, sleep=slept.append)
    assert found.open and slept == [30, 30]


def test_waiting_gives_up_and_says_which_signal() -> None:
    now = [0.0]

    def later(seconds: float) -> None:
        now[0] += seconds

    red = Gate(DEVICE, reasons=("x",), signals=(Signal("playback", "x"),))
    with pytest.raises(GateTimeout, match="playback"):
        wait_until_clear(lambda: red, poll_s=60, timeout_s=120, sleep=later,
                         clock=lambda: now[0])


def test_a_lane_gate_looks_rarely_while_clear_and_gives_up_a_lane_once() -> None:
    now = [0.0]
    looks: list[str] = []
    busy_disks = {OTHER}

    def measure(device: str) -> Activity | None:
        looks.append(device)
        return BUSY if device in busy_disks else QUIET

    def later(seconds: float) -> None:
        now[0] += seconds

    lane = LaneGate(every_s=60, poll_s=30, timeout_s=90, sample_s=0,
                    processes=lambda: [], measure=measure,
                    view=lambda: ServerView(), sleep=later, clock=lambda: now[0])
    lane(DEVICE, "first")
    lane(DEVICE, "second")
    assert looks == [DEVICE], "clear a moment ago is clear enough"
    now[0] += 61
    lane(DEVICE, "third")
    assert looks == [DEVICE, DEVICE]

    with pytest.raises(GateTimeout):
        lane(OTHER, "first")
    looked = len(looks)
    with pytest.raises(GateTimeout):
        lane(OTHER, "second")
    assert len(looks) == looked, "a lane given up is not waited on again"
    assert any(not g.open for g in lane.history)


# ------------------------------------------------------------- the server view
def test_the_server_view_reads_tasks_sessions_and_recent_changes() -> None:
    with running_server() as (url, recorder):
        recorder.scheduled_tasks = [
            {"Id": "t1", "Name": "Scan Media Library", "State": "Running"},
            {"Id": "t2", "Name": "Extract chapter images", "State": "Idle"},
        ]
        recorder.sessions = [playing("/srv/media/Northwind.mkv")]
        recorder.items[0]["DateLastSaved"] = "2026-01-01T11:55:00Z"
        recorder.items[1]["DateLastSaved"] = "2026-01-01T09:00:00Z"
        view = server_view(client_for(url), recent_window_s=600,
                           now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
        assert view.error is None
        assert view.running_tasks == ("Scan Media Library",)
        assert len(view.sessions) == 1
        assert [row["Id"] for row in view.recent] == [recorder.items[0]["Id"]]
        asked = recorder.queries[-1][1]
        assert asked["mindatelastsaved"] == "2026-01-01T11:50:00Z"
        assert not recorder.posted, "the view only reads"


class Unreachable:
    """A client whose every read fails, as one whose server has gone away does."""

    user_id = USER_ID

    def get(self, route: str, **_params: Any) -> Any:
        raise ConnectionError(f"{route}: connection refused")

    def sessions(self) -> list[dict[str, Any]]:
        raise ConnectionError("connection refused")


def test_a_server_view_that_fails_carries_the_error() -> None:
    view = server_view(Unreachable())  # type: ignore[arg-type]
    assert view.error == "/ScheduledTasks: connection refused"
    assert gate(DEVICE, processes=[], server_error=view.error).red_by == ("server",)


def test_the_gate_verb_names_playback_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jfkit.jobs.list_processes", lambda: [])
    monkeypatch.setattr("jfkit.jobs.sample_activity", lambda *_a, **_k: None)
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    media = tmp_path / "Northwind - S01E03.mkv"
    with running_server() as (url, recorder):
        recorder.sessions = [playing(str(media))]
        config = tmp_path / "config.toml"
        config.write_text(
            f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
            f'user_id = "{USER_ID}"\n', encoding="utf-8",
        )
        assert jfkit_cli.main([
            "--config", str(config), "jobs", "gate", str(tmp_path), "--sample", "0",
            "--json",
        ]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["red_by"] == ["playback"]


def test_a_playing_item_without_a_path_is_looked_up_or_noted() -> None:
    with running_server() as (url, recorder):
        item = recorder.items[6]
        recorder.sessions = [{"UserName": "first-fixture-user",
                              "NowPlayingItem": {"Id": item["Id"], "Name": item["Name"]}}]
        view = server_view(client_for(url), recent_window_s=0)
    assert view.sessions[0]["NowPlayingItem"]["Path"] == item["Path"]
    assert gate(DEVICE, processes=[], sessions=view.sessions).red_by == ("playback",)
    unknown = gate(DEVICE, processes=[], sessions=[
        {"NowPlayingItem": {"Name": "Northwind S01E03"}}
    ])
    assert unknown.open and "did not give" in str(unknown)
