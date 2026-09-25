"""Segments, coverage, and the two lookups that must never be hardcoded.

The test worth reading is the coverage one: a series that is nearly covered
is excluded, because analysing the two episodes that are missing means reading
every episode of the series, and that is hours of disk for two openings.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jfkit.jobs import Process
from jfkit.segments import (
    COVERED_FRACTION,
    Ambiguous,
    Task,
    cancel_task,
    coverage,
    find_plugin,
    find_task,
    running,
    scope_plugin,
    segments_for,
    start_task,
    tasks,
)

from tests.fake_server import Recorder, client_for, fake_server

PLUGIN_ID = "00000000-0000-0000-0000-000000000301"
TASK_ID = "00000000-0000-0000-0000-000000000401"
SERIES_ID = "00000000-0000-0000-0000-000000000501"
OTHER_SERIES_ID = "00000000-0000-0000-0000-000000000502"


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running_server:
        yield running_server


@pytest.fixture
def installed(server: tuple[str, Recorder]) -> tuple[str, Recorder]:
    url, recorder = server
    recorder.installed_plugins = [
        {"Id": PLUGIN_ID, "Name": "Segment Finder", "Version": "1.2.3"},
        {"Id": "00000000-0000-0000-0000-000000000302", "Name": "Theme Songs"},
    ]
    recorder.plugin_configuration[PLUGIN_ID] = {
        "SeriesExclusions": [], "MovieExclusions": [], "AutoDetectIntros": True,
        "SomethingElse": "left alone",
    }
    recorder.scheduled_tasks = [
        {"Id": TASK_ID, "Name": "Detect and analyse media segments", "State": "Idle"},
        {"Id": "00000000-0000-0000-0000-000000000402", "Name": "Scan the library",
         "State": "Idle"},
    ]
    return url, recorder


def episode(number: int, series_id: str = SERIES_ID) -> dict[str, Any]:
    return {
        "Id": f"00000000-0000-0000-0000-{600 + number:012d}",
        "Type": "Episode",
        "SeriesId": series_id,
        "SeriesName": "Northwind",
        "IndexNumber": number,
    }


def film(number: int) -> dict[str, Any]:
    return {
        "Id": f"00000000-0000-0000-0000-{700 + number:012d}",
        "Type": "Movie",
        "Name": "The Quiet Harbour",
    }


# ------------------------------------------------------------------- lookup
def test_a_plugin_is_found_by_name_not_by_a_written_down_identifier(
    installed: tuple[str, Recorder]
) -> None:
    """An identifier in a source file addresses nothing on another machine.

    And it does not fail loudly when it does: the call succeeds against
    nothing, which is the failure this lookup exists to prevent.
    """
    url, _ = installed
    found = find_plugin(client_for(url), "segment")
    assert found["Id"] == PLUGIN_ID


def test_a_name_that_matches_nothing_says_what_is_installed(
    installed: tuple[str, Recorder]
) -> None:
    url, _ = installed
    with pytest.raises(LookupError, match="installed: "):
        find_plugin(client_for(url), "nothing like this")


def test_a_name_that_matches_two_things_is_an_error_not_a_coin_toss(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    recorder.installed_plugins.append(
        {"Id": "00000000-0000-0000-0000-000000000303", "Name": "Segment Exporter"}
    )
    with pytest.raises(Ambiguous, match="matches"):
        find_plugin(client_for(url), "segment")


def test_a_task_is_found_the_same_way(installed: tuple[str, Recorder]) -> None:
    url, _ = installed
    assert find_task(client_for(url), "analyse media segments").id == TASK_ID
    assert len(tasks(client_for(url))) == 2


def test_a_running_task_is_reported_as_running(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    recorder.scheduled_tasks[1]["State"] = "Running"
    found = running(client_for(url))
    assert [task.name for task in found] == ["Scan the library"]
    assert found[0].running


# ----------------------------------------------------------------- segments
def test_the_marked_stretches_of_one_item_come_back_as_values(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    item = episode(1)["Id"]
    recorder.media_segments[item] = [
        {"ItemId": item, "Type": "Intro", "StartTicks": 0, "EndTicks": 300_000_000},
    ]
    found = segments_for(client_for(url), item)
    assert len(found) == 1
    assert found[0].kind == "Intro"
    assert found[0].length_ticks == 300_000_000


def test_an_item_with_no_segments_is_an_empty_answer_not_an_error(
    installed: tuple[str, Recorder]
) -> None:
    url, _ = installed
    assert segments_for(client_for(url), episode(2)["Id"]) == []


# ----------------------------------------------------------------- coverage
def test_a_nearly_covered_series_is_treated_as_covered() -> None:
    """The decision the whole module is for.

    Eight of ten episodes are covered. Analysing the other two means reading
    all ten, and on mechanical storage that is hours for two openings.
    """
    episodes = [episode(n) for n in range(10)]
    covered = [e["Id"] for e in episodes[:8]]
    found = coverage(episodes, covered)
    assert found.covered_series == (SERIES_ID,)
    assert found.episodes_to_read == 0


def test_a_series_below_the_bar_is_read_in_full() -> None:
    episodes = [episode(n) for n in range(10)]
    found = coverage(episodes, [e["Id"] for e in episodes[:3]])
    assert found.uncovered_series == (SERIES_ID,)
    assert found.episodes_to_read == 7
    assert "would be read" in str(found)


def test_the_bar_is_an_argument_and_the_default_is_written_down() -> None:
    episodes = [episode(n) for n in range(10)]
    covered = [e["Id"] for e in episodes[:5]]
    assert coverage(episodes, covered, fraction=0.5).covered_series == (SERIES_ID,)
    assert coverage(episodes, covered, fraction=COVERED_FRACTION).uncovered_series


def test_identifiers_are_compared_without_their_separators() -> None:
    """The two places they come from spell them differently."""
    one = episode(1)
    bare = str(one["Id"]).replace("-", "").upper()
    assert coverage([one], [bare]).covered_series == (SERIES_ID,)


def test_films_are_counted_one_at_a_time() -> None:
    films = [film(1), film(2)]
    found = coverage(films, [films[0]["Id"]])
    assert found.covered_movies == (films[0]["Id"],)
    assert found.uncovered_movies == (films[1]["Id"],)


def test_two_series_are_judged_separately() -> None:
    episodes = [episode(n) for n in range(5)]
    episodes += [episode(n, OTHER_SERIES_ID) for n in range(5, 10)]
    found = coverage(episodes, [e["Id"] for e in episodes[:5]])
    assert found.covered_series == (SERIES_ID,)
    assert found.uncovered_series == (OTHER_SERIES_ID,)


# ------------------------------------------------------------------ scoping
def test_scoping_writes_the_exclusions_and_leaves_the_rest_of_the_configuration(
    installed: tuple[str, Recorder], tmp_path: Path
) -> None:
    url, recorder = installed
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    episodes = [episode(n) for n in range(10)]
    found = coverage(episodes, [e["Id"] for e in episodes[:9]])

    report = scope_plugin(client, plugin, found, backup_dir=tmp_path)
    written = recorder.plugin_configuration[PLUGIN_ID]

    assert report.applied
    assert written["SeriesExclusions"] == [SERIES_ID]
    assert written["SomethingElse"] == "left alone"


def test_scoping_turns_off_the_setting_that_undoes_it(
    installed: tuple[str, Recorder], tmp_path: Path
) -> None:
    """Automatic detection analyses everything the exclusions do not cover,
    whenever it likes, which is the opposite of scoping a pass."""
    url, recorder = installed
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    report = scope_plugin(client, plugin, coverage([], []), backup_dir=tmp_path)
    assert recorder.plugin_configuration[PLUGIN_ID]["AutoDetectIntros"] is False
    assert any("opposite of scoping" in note for note in report.notes)


def test_a_dry_run_scope_writes_nothing(installed: tuple[str, Recorder]) -> None:
    url, recorder = installed
    client = client_for(url)
    plugin = find_plugin(client, "segment")
    report = scope_plugin(client, plugin, coverage([], []))
    assert not report.applied
    assert recorder.plugin_configuration[PLUGIN_ID]["AutoDetectIntros"] is True



def test_scoping_keeps_the_exclusions_that_were_already_there(
    installed: tuple[str, Recorder], tmp_path: Path
) -> None:
    """Somebody put them there for a reason the coverage cannot see."""
    url, recorder = installed
    by_hand = "00000000-0000-0000-0000-000000000599"
    recorder.plugin_configuration[PLUGIN_ID]["SeriesExclusions"] = [by_hand, SERIES_ID]
    recorder.plugin_configuration[PLUGIN_ID]["MovieExclusions"] = [by_hand]
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    episodes = [episode(n) for n in range(10)]
    found = coverage(episodes, [e["Id"] for e in episodes])

    report = scope_plugin(client, plugin, found, backup_dir=tmp_path)
    written = recorder.plugin_configuration[PLUGIN_ID]
    assert written["SeriesExclusions"] == [by_hand, SERIES_ID], "no duplicate, none lost"
    assert written["MovieExclusions"] == [by_hand]
    assert report.kept == 3


def test_an_applied_scope_without_a_backup_directory_is_refused(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    before = dict(recorder.plugin_configuration[PLUGIN_ID])
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    with pytest.raises(ValueError, match="backup"):
        scope_plugin(client, plugin, coverage([], []))
    assert ("POST", f"/Plugins/{PLUGIN_ID}/Configuration") not in recorder.requests
    assert recorder.plugin_configuration[PLUGIN_ID] == before


def test_an_exclusion_list_of_another_shape_is_refused_not_overwritten(
    installed: tuple[str, Recorder], tmp_path: Path
) -> None:
    url, recorder = installed
    recorder.plugin_configuration[PLUGIN_ID]["SeriesExclusions"] = "a,b"
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    with pytest.raises(ValueError, match="not a list"):
        scope_plugin(client, plugin, coverage([], []), backup_dir=tmp_path)
    assert ("POST", f"/Plugins/{PLUGIN_ID}/Configuration") not in recorder.requests


def test_the_configuration_as_it_was_is_written_before_the_change(
    installed: tuple[str, Recorder], tmp_path: Path
) -> None:
    url, recorder = installed
    before = dict(recorder.plugin_configuration[PLUGIN_ID])
    client = client_for(url, dry_run=False)
    plugin = find_plugin(client, "segment")
    seen_at_post: list[list[Path]] = []
    real_post = client.post

    def post(route: str, body: object = None, **params: object) -> object:
        seen_at_post.append(sorted(tmp_path.glob("*.json")))
        return real_post(route, body, **params)

    client.post = post  # type: ignore[method-assign]
    report = scope_plugin(client, plugin, coverage([], []), backup_dir=tmp_path)
    assert report.backup is not None
    assert seen_at_post == [[report.backup]], "the artefact existed before the write"
    assert json.loads(report.backup.read_text(encoding="utf-8")) == before


# --------------------------------------------------------------- the switch
def test_a_task_is_not_started_while_the_disk_is_busy(
    installed: tuple[str, Recorder]
) -> None:
    """An analysis pass is a reader. It goes through the same gate as any other."""
    url, recorder = installed
    client = client_for(url, dry_run=False)
    task = find_task(client, "analyse media segments")
    held = start_task(
        client, task, device="/srv",
        processes=[Process(11, "ffmpeg", "ffmpeg -i /srv/media/one.mkv")],
    )
    assert held is not None and not held.open
    assert recorder.tasks_started == []


def test_a_task_starts_when_the_disk_is_quiet(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    client = client_for(url, dry_run=False)
    task = find_task(client, "analyse media segments")
    assert start_task(client, task, device="/srv", processes=[]) is None
    assert recorder.tasks_started == [TASK_ID]


def test_the_servers_own_running_task_holds_the_gate(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    recorder.scheduled_tasks[1]["State"] = "Running"
    client = client_for(url, dry_run=False)
    task = find_task(client, "analyse media segments")
    held = start_task(client, task, device="/srv", processes=[])
    assert held is not None and not held.open
    assert recorder.tasks_started == []


def test_with_no_device_named_nothing_is_checked(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    client = client_for(url, dry_run=False)
    assert start_task(client, Task(id=TASK_ID, name="x")) is None
    assert recorder.tasks_started == [TASK_ID]


def test_a_running_pass_can_be_stopped_without_restarting_anything(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    client = client_for(url, dry_run=False)
    cancel_task(client, find_task(client, "analyse media segments"))
    assert recorder.tasks_cancelled == [TASK_ID]


def test_a_dry_run_client_does_not_stop_anything(
    installed: tuple[str, Recorder]
) -> None:
    url, recorder = installed
    client = client_for(url)
    cancel_task(client, find_task(client, "analyse media segments"))
    assert recorder.tasks_cancelled == []
