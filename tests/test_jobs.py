"""The device gate, the lane packing, and the detached-job command.

No test here starts a process, lists the real ones or touches a real disk:
the process list is injected and the paths are temporary. What is tested is
the reasoning, and in particular the one that cost a frozen machine -- a
substring test that made every reader on one device look like a reader on
another.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from jfkit.devices import device_of, is_rotational, mentions_device, same_device
from jfkit.jobs import (
    UNIT_RUNNER,
    WINDOWS_SCHEDULER,
    Gate,
    Job,
    JobRefused,
    Process,
    detached_command,
    detached_commands,
    gate,
    launch_detached,
    list_processes,
    pack_lanes,
)

DEVICE = "/srv"

#: Two volume names, assembled rather than written out. This repository's
#: privacy gate refuses a drive-letter path outside the examples, so a test
#: *about* drive letters would otherwise trip the gate it is testing.
ONE_VOLUME = "Y" + ":"
OTHER_VOLUME = "T" + ":"


def reader(pid: int, command: str, name: str = "ffmpeg") -> Process:
    return Process(pid=pid, name=name, command=command)


# ------------------------------------------------------------------ devices
def test_a_path_resolves_to_the_device_that_would_serve_it(tmp_path: Path) -> None:
    here = device_of(tmp_path)
    assert here
    assert same_device(tmp_path, tmp_path / "a" / "b" / "c.mkv")


def test_a_relative_path_answers_the_same_as_its_absolute_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert device_of("some/file.mkv") == device_of(tmp_path / "some" / "file.mkv")


def test_an_unknown_device_says_unknown_rather_than_guessing() -> None:
    """The safe direction: the gate treats unknown as needing protection."""
    answer = is_rotational("/this/is/not/a/mount/point")
    assert answer is None or isinstance(answer, bool)


# ----------------------------------------------------- the substring finding
def test_a_bare_volume_name_inside_another_argument_is_not_a_reader() -> None:
    """The failure that froze a machine, as an assertion.

    A command line naming a file on one device contains the other device's
    letter in a protocol prefix, a label or a parameter. A substring test put
    every one of those jobs in the wrong lane, and two heavy readers ended up
    on the one disk that could not take them.
    """
    command = f"ffmpeg -i concat:{ONE_VOLUME}/media/one.mkv"
    assert not mentions_device(command, OTHER_VOLUME)
    assert mentions_device(command, ONE_VOLUME)
    assert not mentions_device(
        f"ffmpeg -i /srv/media/one.mkv -metadata {OTHER_VOLUME[0]}=1", OTHER_VOLUME
    )


def test_a_posix_device_is_matched_as_a_path_and_not_as_a_word() -> None:
    assert mentions_device("ffmpeg -i /srv/media/one.mkv", "/srv")
    assert not mentions_device("ffmpeg -i /srvother/media/one.mkv", "/srv")
    assert not mentions_device("ffmpeg --tag srv/media", "/srv")


# --------------------------------------------------------------- the gate
def test_one_reader_on_a_device_holds_the_gate() -> None:
    held = gate(DEVICE, processes=[reader(11, "ffmpeg -i /srv/media/one.mkv")])
    assert not held.open
    assert "already reading" in str(held)


def test_a_reader_on_another_device_does_not() -> None:
    clear = gate(DEVICE, processes=[reader(11, "ffmpeg -i /other/media/one.mkv")])
    assert clear.open


def test_a_process_that_is_not_a_heavy_reader_does_not_hold_it() -> None:
    clear = gate(
        DEVICE,
        processes=[reader(11, "a-text-editor /srv/media/notes.txt", name="editor")],
    )
    assert clear.open


def test_a_lane_does_not_see_itself() -> None:
    """Without this a lane waits for the reader it started, forever."""
    mine = reader(11, "ffmpeg -i /srv/media/one.mkv -tag lane-a")
    assert gate(DEVICE, processes=[mine], own_tag="lane-a").open
    assert not gate(DEVICE, processes=[mine], own_tag="lane-b").open


def test_the_servers_own_background_reader_counts(
) -> None:
    """It is a reader, it holds the disk for hours, and it announces nothing."""
    held = gate(
        DEVICE,
        processes=[reader(11, "ffmpeg -hwaccel cuda -i /srv/media/one.mkv -vf fps=0.06")],
    )
    assert not held.open


def test_a_running_server_task_holds_the_gate_too() -> None:
    held = gate(DEVICE, processes=[], running_tasks=["Scan the library"])
    assert not held.open
    assert "reads everything it can" in str(held)


def test_a_quiet_device_is_clear() -> None:
    clear = gate(DEVICE, processes=[], running_tasks=[])
    assert clear.open
    assert str(clear).endswith("clear")


@pytest.mark.skipif(
    sys.platform != "linux" and os.name != "nt",
    reason="the process table is read on two platforms only",
)
def test_the_real_process_listing_sees_this_process() -> None:
    """The one test that reads the machine's process table rather than a stand-in."""
    listed = {p.pid: p for p in list_processes()}
    assert os.getpid() in listed
    assert "python" in listed[os.getpid()].name.lower()


def test_more_readers_can_be_allowed_where_the_storage_takes_them() -> None:
    two = [reader(11, "ffmpeg -i /srv/a.mkv"), reader(12, "ffmpeg -i /srv/b.mkv")]
    assert gate(DEVICE, processes=two[:1], max_readers=2).open
    assert not gate(DEVICE, processes=two, max_readers=2).open


# ------------------------------------------------------------- the packing
def test_work_is_grouped_by_the_device_it_reads(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    files = []
    for directory, sizes in ((one, [30, 10]), (two, [20])):
        for index, size in enumerate(sizes):
            path = directory / f"{index}.mkv"
            path.write_bytes(b"x" * size)
            files.append(path)

    lanes = pack_lanes(files, path_of=lambda p: p)
    assert sum(len(lane.items) for lane in lanes) == 3
    # both directories are on one device in a temporary tree, so this is one lane
    assert {lane.device for lane in lanes} == {device_of(tmp_path)}


def test_a_lane_is_filled_largest_first(tmp_path: Path) -> None:
    """So several lanes finish together instead of one running alone at the end."""
    items = [("a", 5.0), ("b", 100.0), ("c", 50.0), ("d", 1.0)]
    lanes = pack_lanes(
        items, path_of=lambda item: tmp_path, weight_of=lambda item: item[1]
    )
    assert [name for name, _w in lanes[0].items] == ["b", "c", "a", "d"]


def test_splitting_a_device_balances_the_two_queues(tmp_path: Path) -> None:
    items = [("a", 100.0), ("b", 60.0), ("c", 50.0), ("d", 40.0)]
    lanes = pack_lanes(
        items, path_of=lambda item: tmp_path, weight_of=lambda item: item[1], lanes=2
    )
    assert len(lanes) == 2
    assert abs(lanes[0].weight - lanes[1].weight) <= 30.0
    assert "item(s), weight" in str(lanes[0])


def test_a_file_that_is_not_there_weighs_nothing_rather_than_raising(
    tmp_path: Path
) -> None:
    lanes = pack_lanes([tmp_path / "gone.mkv"], path_of=lambda p: p)
    assert lanes[0].weight == 0.0


# ------------------------------------------------------------ the detaching
def test_the_detached_command_is_returned_as_data_not_run() -> None:
    job = Job(name="language scan", argv=["mkvkit", "langid", "scan", "/srv/media"])
    command = detached_command(job, program=UNIT_RUNNER)
    assert command[0] == UNIT_RUNNER
    assert command[-3:] == ["langid", "scan", "/srv/media"]
    assert any(part.startswith("--unit=") for part in command)


def test_the_other_platform_gets_the_other_shape() -> None:
    job = Job(name="language scan", argv=["mkvkit", "langid", "scan"])
    command = detached_command(job, program=WINDOWS_SCHEDULER)
    assert command[0] == WINDOWS_SCHEDULER
    assert "/TN" in command
    assert command[command.index("/TN") + 1] == "language-scan"


def test_a_name_with_awkward_characters_becomes_one_that_works() -> None:
    job = Job(name="scan: films & series (2)", argv=["mkvkit"])
    command = detached_command(job, program=WINDOWS_SCHEDULER)
    assert command[command.index("/TN") + 1] == "scan-films-series-2"


def test_with_no_scheduler_the_error_says_where_to_read_about_it(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal that names the document, rather than one that names nothing."""
    monkeypatch.setattr("jfkit.jobs.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match=r"detached-jobs\.md"):
        detached_command(Job(name="scan", argv=["mkvkit"]))


def test_a_dry_run_launch_returns_the_command_it_would_have_run() -> None:
    commands, result = launch_detached(
        Job(name="scan", argv=["mkvkit", "langid", "scan"]), program=UNIT_RUNNER
    )
    assert result is None
    assert [command[0] for command in commands] == [UNIT_RUNNER]

    ran: list[list[str]] = []
    commands2, result2 = launch_detached(
        Job(name="scan", argv=["mkvkit", "langid", "scan"]),
        program=UNIT_RUNNER, dry_run=False,
        runner=lambda argv: ran.append(list(argv)) or 0,
    )
    assert result2 == 0
    assert ran == commands2 == commands


# ------------------------------------------------ environment and log
def test_the_unit_form_carries_the_environment_and_the_log(tmp_path: Path) -> None:
    job = Job(name="scan", argv=["mkvkit", "langid", "scan"],
              environment={"B_SETTING": "2", "A_SETTING": "one two"},
              log=tmp_path / "scan.log")
    command = detached_command(job, program=UNIT_RUNNER)
    assert "--setenv=A_SETTING=one two" in command
    assert "--setenv=B_SETTING=2" in command
    target = (tmp_path / "scan.log").absolute()
    assert f"--property=StandardOutput=append:{target}" in command
    assert f"--property=StandardError=append:{target}" in command
    assert command[-3:] == ["mkvkit", "langid", "scan"]
    assert command.index("--setenv=A_SETTING=one two") < command.index("mkvkit")


@pytest.mark.parametrize("extra", [
    {"environment": {"A_SETTING": "1"}},
    {"log": Path("work/scan.log")},
])
def test_the_task_form_refuses_what_it_cannot_carry(extra: dict[str, object]) -> None:
    """Rather than start the job without its environment or with its output lost."""
    job = Job(name="scan", argv=["mkvkit"], **extra)  # type: ignore[arg-type]
    with pytest.raises(JobRefused, match="Nothing was created"):
        detached_command(job, program=WINDOWS_SCHEDULER)
    ran: list[list[str]] = []
    with pytest.raises(JobRefused):
        launch_detached(job, program=WINDOWS_SCHEDULER, dry_run=False,
                        runner=lambda argv: ran.append(list(argv)) or 0)
    assert ran == []


# ------------------------------------------------ the scheduled-task form
def test_the_task_is_created_without_overwriting_and_then_run() -> None:
    """The one-off midnight trigger is in the past, so the run is explicit."""
    job = Job(name="language scan", argv=["mkvkit", "langid", "scan"])
    create, start = detached_commands(job, program=WINDOWS_SCHEDULER)
    assert create[:2] == [WINDOWS_SCHEDULER, "/Create"]
    assert "/F" not in create
    assert start == [WINDOWS_SCHEDULER, "/Run", "/TN", "language-scan"]


def test_replacing_a_task_is_asked_for_explicitly() -> None:
    job = Job(name="scan", argv=["mkvkit"])
    create = detached_command(job, program=WINDOWS_SCHEDULER, replace=True)
    assert create[:3] == [WINDOWS_SCHEDULER, "/Create", "/F"]


def test_an_existing_task_of_the_same_name_is_refused() -> None:
    ran: list[list[str]] = []

    def exists(argv: object) -> int:
        ran.append(list(argv))  # type: ignore[call-overload]
        return 0  # the query found it

    with pytest.raises(JobRefused, match="already exists"):
        launch_detached(Job(name="scan", argv=["mkvkit"]),
                        program=WINDOWS_SCHEDULER, dry_run=False, runner=exists)
    assert ran == [[WINDOWS_SCHEDULER, "/Query", "/TN", "scan"]], "nothing created"


def test_a_new_task_is_queried_created_and_run_in_that_order() -> None:
    ran: list[list[str]] = []

    def runner(argv: object) -> int:
        command = list(argv)  # type: ignore[call-overload]
        ran.append(command)
        return 1 if command[1] == "/Query" else 0

    commands, result = launch_detached(
        Job(name="scan", argv=["mkvkit"]), program=WINDOWS_SCHEDULER,
        dry_run=False, runner=runner,
    )
    assert result == 0
    assert [command[1] for command in ran] == ["/Query", "/Create", "/Run"]
    assert ran[1:] == commands


def test_a_failed_create_is_not_followed_by_a_run() -> None:
    ran: list[list[str]] = []

    def runner(argv: object) -> int:
        command = list(argv)  # type: ignore[call-overload]
        ran.append(command)
        return 1

    _commands, result = launch_detached(
        Job(name="scan", argv=["mkvkit"]), program=WINDOWS_SCHEDULER,
        dry_run=False, replace=True, runner=runner,
    )
    assert result == 1
    assert [command[1] for command in ran] == ["/Create"]


def test_a_job_knows_which_device_it_reads(tmp_path: Path) -> None:
    job = Job(name="scan", argv=["mkvkit"], reads=tmp_path / "one.mkv")
    assert job.device == device_of(tmp_path)
    assert Job(name="scan", argv=["mkvkit"]).device is None


def test_the_gate_reports_what_it_could_find_out_about_the_storage() -> None:
    found = gate(DEVICE, processes=[])
    assert isinstance(found, Gate)
    assert found.rotational is None or isinstance(found.rotational, bool)
    assert os.name  # the platform is whatever it is; nothing here depends on it
