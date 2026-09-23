"""The record round-trip, and the comparison that follows every operation.

Two things are worth the reading here. The first is the test that sends a
record back with one field changed and then checks that a field nobody
mentioned came back unchanged -- which is the whole reason the round-trip
sends the entire object. The second is the chapter comparison: a file whose
marks are unnamed differs from itself in every one of those strings, and a
comparison that reports that is a comparison nobody reads twice.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit.dto import (
    COPIES,
    DISCARDS,
    TICKS_PER_SECOND,
    chapter_rows,
    compare,
    fetch,
    load,
    normalise_chapter_name,
    phases_for,
    save,
    update_item,
    user_data,
)

from tests.fake_server import (
    ITEMS,
    SECOND_USER_ID,
    UPDATE_COPIES,
    UPDATE_DISCARDS,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)

FIRST = ITEMS[0]["Id"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


# --------------------------------------------------------------- the table
def test_the_copies_and_discards_tables_are_what_the_server_does(
    server: tuple[str, Recorder]
) -> None:
    """The published table is asserted against a server, not against a memory.

    Every field the table claims is copied is sent, and read back; every field
    it claims is discarded is sent with a different value, and read back
    unchanged. A table like this is worth publishing only if something checks
    it, because its failure mode is silence.
    """
    url, _recorder = server
    client = client_for(url, dry_run=False)

    before = fetch(client, FIRST)
    body = dict(before)
    body.pop("Trickplay", None)
    body["Name"] = "Blue Meridian"
    body["Path"] = "/srv/media/movies/somewhere-else"
    client.post(f"/Items/{FIRST}", body)
    after = fetch(client, FIRST)

    assert after["Name"] == "Blue Meridian"
    assert after["Path"] == before["Path"], "the path is discarded, as the table says"

    assert COPIES >= UPDATE_COPIES, sorted(UPDATE_COPIES - COPIES)
    assert DISCARDS >= UPDATE_DISCARDS, sorted(UPDATE_DISCARDS - DISCARDS)
    assert not (COPIES & DISCARDS)


def test_a_field_left_out_of_the_body_is_emptied(
    server: tuple[str, Recorder]
) -> None:
    """The finding the whole module exists for.

    Sending a body with two fields in it does not change two fields. It
    changes two fields and empties every other field the route assigns, which
    on this item means the overview and the provider identifiers.
    """
    url, _ = server
    client = client_for(url, dry_run=False)
    before = fetch(client, FIRST)
    assert before["Overview"] and before["ProviderIds"]

    client.post(f"/Items/{FIRST}", {"Name": "Winter Ledger"})
    after = fetch(client, FIRST)

    assert after["Name"] == "Winter Ledger"
    assert after["Overview"] is None
    assert not after["ProviderIds"]


def test_the_preview_block_has_to_come_out_first(
    server: tuple[str, Recorder]
) -> None:
    """Sending the record back as it arrived fails, and says nothing useful."""
    from jfkit.errors import ServerRefused

    url, _ = server
    client = client_for(url, dry_run=False)
    whole = fetch(client, FIRST)
    assert "Trickplay" in whole
    with pytest.raises(ServerRefused) as caught:
        client.post(f"/Items/{FIRST}", whole)
    assert caught.value.status == 500


def test_update_item_keeps_everything_it_was_not_asked_about(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    before = fetch(client, FIRST)

    update_item(client, FIRST, {"Name": "Quiet Foundry"})
    after = fetch(client, FIRST)

    assert after["Name"] == "Quiet Foundry"
    assert after["Overview"] == before["Overview"]
    assert after["ProviderIds"] == before["ProviderIds"]
    assert all("Trickplay" not in body for _id, body in recorder.posted)


def test_writing_a_discarded_field_is_refused_rather_than_ignored(
    server: tuple[str, Recorder]
) -> None:
    """Asking for something that cannot work fails loudly.

    The route accepts a path and drops it. A caller who writes one and reads
    a success has been told nothing, so this refuses before sending.
    """
    url, _ = server
    client = client_for(url, dry_run=False)
    with pytest.raises(ValueError, match="discards these"):
        update_item(client, FIRST, {"Path": "/srv/media/movies/elsewhere"})


# -------------------------------------------------------------- phase order
def test_the_phases_come_out_in_the_order_that_survives() -> None:
    plan = phases_for(
        {"IndexNumber": 3, "Name": "Quiet Ledger", "PremiereDate": "1994-01-01",
         "LockData": True},
        item_type="Episode",
    )
    assert [p.name for p in plan] == ["numbers", "refresh", "names", "dates", "lock"]
    assert plan[0].fields == {"IndexNumber": 3}
    assert plan[3].fields == {"PremiereDate": "1994-01-01"}


def test_a_date_is_never_written_before_a_refresh() -> None:
    """The ordering rule, as an assertion rather than a paragraph.

    A refresh re-seeds an empty air date from the container's creation time,
    so a date written before one does not survive it.
    """
    plan = phases_for({"PremiereDate": "1994-01-01", "IndexNumber": 2})
    names = [p.name for p in plan]
    assert names.index("refresh") < names.index("dates")


def test_with_nothing_to_write_first_there_is_nothing_to_refresh_between() -> None:
    plan = phases_for({"Name": "Blue Anchor"})
    assert [p.name for p in plan] == ["names"]


def test_the_lock_is_refused_on_anything_that_cascades() -> None:
    for kind in ("Series", "Season", "BoxSet", "Folder"):
        with pytest.raises(ValueError, match="every item underneath"):
            phases_for({"LockData": True}, item_type=kind)
    assert phases_for({"LockData": True}, item_type="Episode")


def test_the_refresh_phase_runs_between_the_writes(
    server: tuple[str, Recorder]
) -> None:
    url, _recorder = server
    client = client_for(url, dry_run=False)
    seen: list[str] = []

    def refresh(item_id: str) -> None:
        seen.append(item_id)

    plan = update_item(
        client, FIRST, {"IndexNumber": 4, "Name": "Quiet Aviary"}, refresh=refresh
    )
    assert seen == [FIRST]
    assert [p.name for p in plan] == ["numbers", "refresh", "names"]
    assert fetch(client, FIRST)["Name"] == "Quiet Aviary"


def test_a_dry_run_client_sends_nothing(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    client = client_for(url)
    update_item(client, FIRST, {"Name": "Quiet Quarry"})
    assert recorder.posted == []
    assert fetch(client, FIRST)["Name"] == ITEMS[0]["Name"]


# -------------------------------------------------------------- comparison
def test_a_generated_chapter_name_is_not_a_name() -> None:
    for generated in ("Chapter 1", "chapter 01", "Kapitel 7", "Scene 12", "Teil 3"):
        assert normalise_chapter_name(generated) is None
    assert normalise_chapter_name("The Quiet Harbour") == "The Quiet Harbour"
    assert normalise_chapter_name("") is None
    assert normalise_chapter_name(None) is None


def test_two_identical_records_compare_clean() -> None:
    item = dict(ITEMS[0])
    assert compare(item, dict(item)).ok


def test_renumbered_generated_marks_are_not_a_difference() -> None:
    """The normalisation rule, doing the job it exists for.

    The same marks at the same times, named by two different generators, is
    not a change. Without the rule this reports one difference per mark and
    the real ones are lost in the middle of them.
    """
    before = {"Id": "00000000-0000-0000-0000-000000000001",
              "Chapters": [{"StartPositionTicks": 0, "Name": "Chapter 1"},
                           {"StartPositionTicks": 600 * TICKS_PER_SECOND,
                            "Name": "Chapter 2"}]}
    after = {"Id": before["Id"],
             "Chapters": [{"StartPositionTicks": 0, "Name": "Kapitel 01"},
                          {"StartPositionTicks": 600 * TICKS_PER_SECOND,
                           "Name": "Scene 2"}]}
    assert compare(before, after).ok
    assert chapter_rows(after) == [(0, None), (600 * TICKS_PER_SECOND, None)]


def test_a_mark_that_moved_is_reported_in_milliseconds() -> None:
    before = {"Id": "x", "Chapters": [{"StartPositionTicks": 0, "Name": "Cold Open"}]}
    after = {"Id": "x",
             "Chapters": [{"StartPositionTicks": 2 * TICKS_PER_SECOND,
                           "Name": "Cold Open"}]}
    found = compare(before, after)
    assert not found.ok
    assert "+2000.0 ms" in str(found)


def test_a_mark_inside_the_tolerance_is_not_a_difference() -> None:
    before = {"Id": "x", "Chapters": [{"StartPositionTicks": 0}]}
    after = {"Id": "x", "Chapters": [{"StartPositionTicks": 5000}]}   # half a ms
    assert compare(before, after).ok


def test_an_expected_change_is_reported_separately_rather_than_hidden() -> None:
    before = dict(ITEMS[0])
    after = dict(before, Name="Quiet Bastion")
    found = compare(before, after, expected=["Name"])
    assert found.ok
    assert [d.where for d in found.expected] == ["Name"]


def test_a_changed_stream_table_is_a_difference() -> None:
    before = {"Id": "x", "MediaStreams": [
        {"Type": "Audio", "Index": 1, "Language": "eng", "IsDefault": True},
        {"Type": "Audio", "Index": 2, "Language": "deu"},
    ]}
    after = {"Id": "x", "MediaStreams": [
        {"Type": "Audio", "Index": 1, "Language": "eng", "IsDefault": True},
    ]}
    found = compare(before, after)
    assert not found.ok
    assert "'2 stream(s)' -> '1 stream(s)'" in str(found)


def test_a_changed_identifier_says_what_it_means() -> None:
    before = {"Id": "00000000-0000-0000-0000-000000000001"}
    after = {"Id": "00000000-0000-0000-0000-000000000009"}
    found = compare(before, after)
    assert not found.ok
    assert any("play state does not follow" in note for note in found.notes)


def test_play_state_moving_is_a_difference() -> None:
    before = {"Id": "x", "UserData": {"PlayCount": 2, "PlaybackPositionTicks": 100}}
    after = {"Id": "x", "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0}}
    found = compare(before, after)
    assert {d.where for d in found.differences} == {
        "UserData.PlayCount", "UserData.PlaybackPositionTicks"
    }


# ------------------------------------------------------------- the artefact
def test_a_record_saved_and_loaded_is_the_same_record(tmp_path: Path) -> None:
    out = save(ITEMS[1], tmp_path / "before" / "record.json")
    assert out.is_file()
    assert load(out) == ITEMS[1]
    assert json.loads(out.read_text(encoding="utf-8"))["Id"] == ITEMS[1]["Id"]


def test_update_item_writes_a_rollback_artefact(
    server: tuple[str, Recorder], tmp_path: Path
) -> None:
    """Every write leaves the record it overwrote on disk, or it is not a write."""
    url, _ = server
    client = client_for(url, dry_run=False)
    backup = tmp_path / "before.json"
    update_item(client, FIRST, {"Name": "Quiet Pilgrim"}, backup=backup)
    assert load(backup)["Name"] == ITEMS[0]["Name"]


def test_play_state_is_read_for_every_user_not_just_the_one_running_this(
    server: tuple[str, Recorder]
) -> None:
    """A position belonging to somebody else is still a position.

    Anything that deletes a row or moves a file checks this first, because the
    administrator's own view of an item says nothing about the other
    people who may be halfway through it.
    """
    url, recorder = server
    client = client_for(url)
    recorder.user_data[(SECOND_USER_ID, FIRST)] = {
        "PlayCount": 1, "PlaybackPositionTicks": 42 * TICKS_PER_SECOND, "Played": False
    }
    found = user_data(client, FIRST, [USER_ID, SECOND_USER_ID])
    assert SECOND_USER_ID in found
    assert found[SECOND_USER_ID]["PlaybackPositionTicks"] == 42 * TICKS_PER_SECOND
