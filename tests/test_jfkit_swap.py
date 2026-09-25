"""The swap procedure: chunking, the outage, and what the record says afterwards.

The files here are a few bytes each and the "service" is a controller that
says yes, so what is tested is the ordering and the verification rather than
the copying -- which the file-side package tests on its own.

The two worth reading: a chunk is sized in bytes rather than in files, because
a hundred episodes and a hundred films are the same file count and two very
different outages; and the verification is a comparison of the record, because
a rebuilt file legitimately has a different size and a size check therefore
proves nothing.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit.dto import fetch
from jfkit.service import ManualServiceController
from jfkit.swap import Pair, chunks, expected_streams, replay_play_state, swap

from tests.fake_server import (
    ITEMS,
    SECOND_USER_ID,
    THIRD_USER_ID,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)

FIRST = ITEMS[0]["Id"]
SECOND = ITEMS[1]["Id"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


@pytest.fixture
def controller() -> ManualServiceController:
    return ManualServiceController(name="the stand-in", confirm=lambda _prompt: True)


def no_sleep(_seconds: float) -> None:
    return None


def opens_fine(_arrived: Path) -> list[str]:
    """The file-side check, stubbed out: these fixtures are not media files.

    The real default reads the file that arrived with an external program and
    compares its track table with the replacement's; that behaviour belongs to
    the file-side package and is tested there, against files built for it.
    """
    return []


def pair(
    tmp_path: Path, item_id: str, name: str, size: int = 16,
    recorder: Recorder | None = None, streams: int | None = None,
) -> Pair:
    """A live file and its rebuild; with a recorder, the item is made to match.

    Matching means the catalogue's path is the live file's, and a refresh
    that has caught up shows the rebuild's stream table -- one German track
    fewer -- because that is what the verification waits for.
    """
    live = tmp_path / "live" / name
    rebuilt = tmp_path / "staging" / name
    live.parent.mkdir(parents=True, exist_ok=True)
    rebuilt.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"o" * size)
    rebuilt.write_bytes(b"n" * (size * 2))
    if recorder is not None:
        item = recorder.find(item_id)
        assert item is not None
        item["Path"] = str(live)
        recorder.refresh_effect.setdefault(item_id, {}).setdefault(
            "MediaStreams", item["MediaStreams"][:2]
        )
    return Pair(item_id=item_id, keeper=live, replacement=rebuilt, streams=streams)


# ------------------------------------------------------------------ chunking
def test_chunks_are_sized_in_bytes_and_not_in_files(tmp_path: Path) -> None:
    pairs = [
        pair(tmp_path, FIRST, "one.mkv", size=int(0.6 * 2**30) // 2),
        pair(tmp_path, SECOND, "two.mkv", size=int(0.6 * 2**30) // 2),
    ]
    assert len(chunks(pairs, chunk_gib=1.0)) == 2
    assert len(chunks(pairs, chunk_gib=4.0)) == 1


def test_a_single_pair_larger_than_the_chunk_still_gets_a_chunk(
    tmp_path: Path
) -> None:
    """Otherwise the largest file in the collection can never be swapped."""
    pairs = [pair(tmp_path, FIRST, "one.mkv", size=2**20)]
    planned = chunks(pairs, chunk_gib=0.000001)
    assert len(planned) == 1
    assert len(planned[0].pairs) == 1


def test_a_time_budget_makes_the_chunks_smaller(tmp_path: Path) -> None:
    """Estimating the outage before the service goes down is the only useful time."""
    pairs = [
        pair(tmp_path, FIRST, "one.mkv", size=30 * 2**20),
        pair(tmp_path, SECOND, "two.mkv", size=30 * 2**20),
    ]
    generous = chunks(pairs, chunk_gib=1.0)
    tight = chunks(pairs, chunk_gib=1.0, mib_per_second=60.0, budget_s=1.0)
    assert len(generous) == 1
    assert len(tight) == 2
    assert tight[0].estimated_seconds(60.0) == pytest.approx(1.0, abs=0.2)


def test_a_chunk_says_what_it_is() -> None:
    planned = chunks([], chunk_gib=1.0)
    assert planned == []


# ------------------------------------------------------------------ dry run
def test_the_dry_run_says_where_every_original_would_go(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    client = client_for(url)
    report = swap(
        client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
        mib_per_second=100.0,
    )
    assert not report.applied
    assert report.outcomes[0].parked is not None
    assert "nothing was moved" in str(report)
    assert (tmp_path / "live" / "one.mkv").read_bytes() == b"o" * 16
    assert recorder.refreshed == []


# ------------------------------------------------------------------- the run
def test_a_swap_keeps_the_identity_and_parks_the_original(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    """The point of swapping in place rather than putting the rebuild beside it."""
    url, recorder = server
    client = client_for(url, dry_run=False)
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    before_id = fetch(client, FIRST)["Id"]

    report = swap(
        client, [one], controller=controller, parked=tmp_path / "parked",
        sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )

    assert report.applied and report.ok, str(report)
    assert report.swapped == 1
    assert one.keeper.read_bytes() == b"n" * 32, "the rebuild is at the live path"
    assert report.outcomes[0].parked is not None
    assert report.outcomes[0].parked.read_bytes() == b"o" * 16
    assert fetch(client, FIRST)["Id"] == before_id
    assert recorder.refreshed == [FIRST]


def test_the_service_is_stopped_and_started_again(
    server: tuple[str, Recorder], tmp_path: Path
) -> None:
    prompts: list[str] = []

    def confirm(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    url, recorder = server
    client = client_for(url, dry_run=False)
    swap(
        client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=ManualServiceController(name="the stand-in", confirm=confirm),
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert len(prompts) == 2
    assert "Stop" in prompts[0] and "Start" in prompts[1]


def test_the_outage_is_measured_and_reported(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    report = swap(
        client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
        sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert report.downtime_s[1] >= 0.0
    assert "service down" in str(report)


def test_nobody_is_watching_before_anything_stops(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    """One call, and it is the failure people remember when it is skipped."""
    url, recorder = server
    recorder.sessions = [
        {"NowPlayingItem": {"Id": FIRST}, "PlayState": {"IsPaused": False}}
    ]
    client = client_for(url, dry_run=False)
    with pytest.raises(TimeoutError, match="still playing"):
        swap(
            client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
            controller=controller, parked=tmp_path / "parked",
            wait_timeout_s=0.0, sleep=no_sleep, poll_s=0.0,
        )
    assert (tmp_path / "live" / "one.mkv").read_bytes() == b"o" * 16


def test_a_record_that_drifts_is_a_problem_even_though_the_file_arrived(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    """The verification that a size check cannot do.

    The file is in place and reads back fine. The refresh rewrote the name
    from the container's own title, which is exactly the outcome that has to
    be caught here rather than noticed months later.
    """
    url, recorder = server
    recorder.refresh_effect[FIRST] = {"Name": "the container's own title"}
    client = client_for(url, dry_run=False)
    report = swap(
        client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
        sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert report.swapped == 1
    assert not report.ok
    assert "Name" in str(report)


def test_a_failed_pair_stops_the_batch_by_default(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    broken = pair(tmp_path, FIRST, "one.mkv", size=1024, recorder=recorder)
    second = pair(tmp_path, SECOND, "two.mkv", size=1024, recorder=recorder)
    untouched = second.keeper.read_bytes()

    def fails_the_first(arrived: Path) -> list[str]:
        return ["the track table is not the rebuild's"] if arrived.name == "one.mkv" else []

    report = swap(
        client, [broken, second], controller=controller, parked=tmp_path / "parked",
        chunk_gib=0.000001, sleep=no_sleep, poll_s=0.0, check=fails_the_first,
    )
    assert not report.ok
    assert report.swapped == 0
    assert "stopped after chunk 1" in str(report)
    assert second.keeper.read_bytes() == untouched, "the second chunk never ran"


def test_a_settle_condition_is_something_true_of_the_new_file_only() -> None:
    settled = expected_streams(3)
    assert settled({"MediaStreams": [{}, {}, {}]})
    assert not settled({"MediaStreams": [{}, {}]})
    assert not settled({})


# ----------------------------------------------------------------- playstate
def test_play_state_is_put_back_where_it_did_not_survive(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    kept = {"PlayCount": 3, "PlaybackPositionTicks": 42, "Played": False}
    restored = replay_play_state(client, FIRST, {SECOND_USER_ID: kept})
    assert restored == [SECOND_USER_ID]
    assert recorder.user_data[(SECOND_USER_ID, FIRST)] == kept


def test_play_state_that_survived_is_left_alone(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    current = fetch(client, FIRST)["UserData"]
    assert replay_play_state(client, FIRST, {USER_ID: current}) == []
    assert recorder.user_data == {}


def test_every_users_position_is_snapshotted_not_just_the_one_running_this(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, FIRST)] = {
        "PlayCount": 1, "PlaybackPositionTicks": 99, "Played": False
    }
    client = client_for(url, dry_run=False)
    report = swap(
        client, [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
        users=[USER_ID, SECOND_USER_ID], sleep=no_sleep, poll_s=0.0,
        check=opens_fine,
    )
    assert report.ok, str(report)
    assert recorder.user_data[(SECOND_USER_ID, FIRST)]["PlaybackPositionTicks"] == 99


def test_a_swap_reports_one_line_per_chunk(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    pairs = [
        pair(tmp_path, FIRST, "one.mkv", size=1024, recorder=recorder),
        pair(tmp_path, SECOND, "two.mkv", size=1024, recorder=recorder),
    ]
    report = swap(
        client, pairs, controller=controller, parked=tmp_path / "parked",
        chunk_gib=0.000001, sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert len(report.chunks) == 2
    assert str(report).count("chunk ") >= 2
    assert report.swapped == 2


# -------------------------------------------------- the plan names the item
def test_a_plan_whose_path_is_not_the_items_is_refused_before_anything_stops(
    server: tuple[str, Recorder], tmp_path: Path
) -> None:
    """A plan line pairing one item's identifier with another item's file."""
    url, recorder = server
    prompts: list[str] = []
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    recorder.items[0]["Path"] = "/srv/media/movies/Blue Canyon (1998)/keeper.mkv"

    report = swap(
        client_for(url, dry_run=False), [one],
        controller=ManualServiceController(
            name="the stand-in", confirm=lambda p: prompts.append(p) or True
        ),
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert not report.ok and report.swapped == 0
    assert "the plan's path is not the item's" in str(report)
    assert prompts == [], "the service was never stopped"
    assert one.keeper.read_bytes() == b"o" * 16
    assert one.replacement.read_bytes() == b"n" * 32
    assert not (tmp_path / "parked").exists()
    assert recorder.refreshed == []


# ------------------------------------------------ the record has to catch up
def test_verification_waits_past_the_stale_reads(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    """The first reads after a refresh are the record from before it."""
    url, recorder = server
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    recorder.stale_reads[FIRST] = 3
    report = swap(
        client_for(url, dry_run=False), [one], controller=controller,
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert report.ok, str(report)
    outcome = report.outcomes[0]
    assert outcome.refresh is not None and outcome.refresh.settled
    assert outcome.refresh.polls == 4
    assert outcome.comparison is not None
    assert any(d.where.startswith("MediaStreams") for d in outcome.comparison.expected)


def test_a_record_that_never_catches_up_is_a_problem_not_a_pass(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    recorder.stale_reads[FIRST] = 10_000
    report = swap(
        client_for(url, dry_run=False), [one], controller=controller,
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0,
        settle_timeout_s=0.0, check=opens_fine,
    )
    assert report.swapped == 1
    assert not report.ok
    assert "had not caught up" in str(report)


def test_the_plans_stream_count_is_the_settle_condition(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    """A record showing a different table, but not the count the plan expects."""
    url, recorder = server
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder, streams=1)
    report = swap(
        client_for(url, dry_run=False), [one], controller=controller,
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0,
        settle_timeout_s=0.0, check=opens_fine,
    )
    assert not report.ok
    assert "had not caught up" in str(report)


# ------------------------------------------------------ everybody's position
def test_naming_nobody_snapshots_every_user(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    recorder.user_data[(THIRD_USER_ID, FIRST)] = {
        "PlayCount": 1, "PlaybackPositionTicks": 77, "Played": False
    }
    report = swap(
        client_for(url, dry_run=False), [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
        sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert report.ok, str(report)
    read = {route for _method, route in recorder.requests}
    for user in (USER_ID, SECOND_USER_ID, THIRD_USER_ID):
        assert f"/Users/{user}/Items/{FIRST}" in read


@pytest.mark.parametrize("broken", ["empty", "failing"])
def test_a_user_list_that_cannot_be_had_refuses_the_swap(
    server: tuple[str, Recorder], tmp_path: Path, broken: str
) -> None:
    url, recorder = server
    if broken == "empty":
        recorder.users = []
    else:
        recorder.users_status = 500
    prompts: list[str] = []
    one = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    report = swap(
        client_for(url, dry_run=False), [one],
        controller=ManualServiceController(
            name="the stand-in", confirm=lambda p: prompts.append(p) or True
        ),
        parked=tmp_path / "parked", sleep=no_sleep, poll_s=0.0, check=opens_fine,
    )
    assert not report.ok and report.swapped == 0
    assert "refused" in str(report) and "user" in str(report)
    assert prompts == []
    assert one.keeper.read_bytes() == b"o" * 16


# ------------------------------------------------ the dry run reads everything
def test_the_dry_run_reports_what_the_apply_would_refuse(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    good = pair(tmp_path, FIRST, "one.mkv", recorder=recorder)
    wrong = pair(tmp_path, SECOND, "two.mkv", recorder=recorder)
    recorder.items[1]["Path"] = "/srv/media/movies/Winter Tide (2011)/keeper.mkv"
    gone = pair(tmp_path, ITEMS[2]["Id"], "three.mkv", recorder=recorder)
    gone.replacement.unlink()

    report = swap(
        client_for(url), [good, wrong, gone], controller=controller,
        parked=tmp_path / "parked",
    )
    assert not report.applied and not report.ok
    by_id = {o.pair.item_id: o for o in report.outcomes}
    assert by_id[FIRST].ok
    assert any("path is not the item's" in p for p in by_id[SECOND].problems)
    assert any("replacement is not there" in p for p in by_id[gone.item_id].problems)
    assert "every precondition was read" in str(report)
    assert [r for r in recorder.requests if r[0] != "GET"] == []
    assert good.keeper.read_bytes() == b"o" * 16


def test_the_dry_run_says_when_the_users_cannot_be_listed(
    server: tuple[str, Recorder], controller: ManualServiceController, tmp_path: Path
) -> None:
    url, recorder = server
    recorder.users = []
    report = swap(
        client_for(url), [pair(tmp_path, FIRST, "one.mkv", recorder=recorder)],
        controller=controller, parked=tmp_path / "parked",
    )
    assert not report.ok
    assert "listed no users" in str(report)
