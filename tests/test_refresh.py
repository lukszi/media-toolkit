"""Asking for a refresh, waiting for it, and the two guards around it.

The test worth reading is the one where the refresh is queued and the record
does not change for three reads. Everything that reads the record once
afterwards and reports success passes on a server that has not started the
work yet, which is exactly how a large batch comes back green and
wrong.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jfkit.dto import fetch
from jfkit.refresh import (
    NotifyRefused,
    apply_identity,
    nfo_guard,
    notify_changed,
    request_refresh,
    safe_refresh,
)

from tests.fake_server import ITEMS, Recorder, client_for, fake_server

FIRST = ITEMS[0]["Id"]


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def no_sleep(_seconds: float) -> None:
    """Time passes instantly here; the waiting is the server's job, not the test's."""
    return None


# ---------------------------------------------------------------- the call
def test_a_refresh_does_not_replace_unless_it_is_told_to(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    request_refresh(client, FIRST)
    route = [r for r in recorder.requests if r[0] == "POST"][-1][1]
    assert route == f"/Items/{FIRST}/Refresh"
    assert recorder.refreshed == [FIRST]


def test_asking_for_replacement_is_logged_as_the_loss_it_is(
    server: tuple[str, Recorder], caplog: pytest.LogCaptureFixture
) -> None:
    url, _ = server
    client = client_for(url, dry_run=False)
    with caplog.at_level("WARNING"):
        request_refresh(client, FIRST, replace_all_metadata=True)
    assert "edited by hand" in caplog.text


def test_a_dry_run_refresh_sends_nothing_and_says_so(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    report = safe_refresh(client_for(url), FIRST)
    assert recorder.refreshed == []
    assert not report.requested
    assert "dry run" in str(report)


# ------------------------------------------------------------- the waiting
def test_a_queued_refresh_is_waited_for_rather_than_assumed(
    server: tuple[str, Recorder]
) -> None:
    """The finding: the call returns at once and the work has not started.

    Three reads answer with the record from before. A check that reads once
    and believes it reports the old stream table as the new one.
    """
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.stale_reads[FIRST] = 3
    recorder.refresh_effect[FIRST] = {"Name": "Quiet Meridian"}

    report = safe_refresh(
        client, FIRST,
        expected_changes=["Name"],
        until=lambda item: item["Name"] == "Quiet Meridian",
        poll_s=0.0, timeout_s=30.0, sleep=no_sleep,
    )
    assert report.settled
    assert report.polls == 4
    assert report.ok


def test_a_refresh_that_never_catches_up_says_so_instead_of_passing(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    client = client_for(url, dry_run=False)
    report = safe_refresh(
        client, FIRST,
        until=lambda item: item["Name"] == "something it will never be",
        poll_s=0.0, timeout_s=0.0, sleep=no_sleep,
    )
    assert not report.settled
    assert not report.ok
    assert "had not caught up" in str(report)


def test_a_refresh_that_rewrites_a_hand_corrected_name_is_caught(
    server: tuple[str, Recorder]
) -> None:
    """The reason the comparison is not optional.

    Nobody asked for the name to change. A refresh changed it, because the
    library is configured to believe the container's own title, and without a
    diff the only symptom is somebody noticing months later.
    """
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.refresh_effect[FIRST] = {"Name": "the container's own title"}

    report = safe_refresh(client, FIRST, expected_changes=["Overview"])
    assert not report.ok
    assert "Name" in str(report.comparison)


def test_an_expected_change_does_not_count_as_drift(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.refresh_effect[FIRST] = {"Overview": "A newly fetched description."}
    report = safe_refresh(client, FIRST, expected_changes=["Overview"])
    assert report.ok
    assert "nothing else moved" in str(report)


# -------------------------------------------------------------- the nudge
def test_a_path_notification_names_the_file(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    sent = notify_changed(
        client,
        ["/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv"],
        roots=["/srv/media/movies", "/srv/media/series"],
    )
    assert len(sent) == 1
    assert recorder.notifications[0]["UpdateType"] == "Modified"


def test_notifying_a_library_root_is_refused(server: tuple[str, Recorder]) -> None:
    """The rule that keeps an hour of disk from happening by accident.

    A notification naming a root is not a notification, it is a full
    validation of everything below it.
    """
    url, recorder = server
    client = client_for(url, dry_run=False)
    with pytest.raises(NotifyRefused, match="library root"):
        notify_changed(client, ["/srv/media/movies"], roots=["/srv/media/movies"])
    assert recorder.notifications == []


def test_notifying_something_above_a_root_is_refused_too(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    client = client_for(url, dry_run=False)
    with pytest.raises(NotifyRefused):
        notify_changed(client, ["/srv/media"], roots=["/srv/media/movies"])


def test_an_unknown_kind_of_change_is_refused(server: tuple[str, Recorder]) -> None:
    url, _ = server
    with pytest.raises(ValueError, match="not one of"):
        notify_changed(client_for(url), ["/srv/media/movies/a"], kind="Rewritten")


# ------------------------------------------------------------- the guards
def test_a_sidecar_beside_the_media_means_clearing_the_name_will_not_work(
    tmp_path: Path
) -> None:
    """The guard, in the shape the finding actually has.

    The recipe is "clear the name, refresh, let a provider fill it". It
    fails wherever the server has written a sidecar, because the local reader
    runs first and puts back the name that was being removed.
    """
    media = tmp_path / "the-quiet-harbour.mkv"
    media.write_bytes(b"")
    (tmp_path / "the-quiet-harbour.nfo").write_text("<movie/>", encoding="utf-8")

    notes = nfo_guard({"Path": str(media)})
    assert notes and "Set the name instead" in notes[0]

    (tmp_path / "the-quiet-harbour.nfo").unlink()
    assert nfo_guard({"Path": str(media)}) == []


def test_the_guard_says_nothing_about_an_item_with_no_path() -> None:
    assert nfo_guard({"Name": "Northwind"}) == []


def test_applying_an_identity_puts_back_the_dates_it_empties(
    server: tuple[str, Recorder]
) -> None:
    """The second guard, and the more expensive one to discover by hand.

    Applying a searched-for identity is a replacing operation: it empties the
    air date and the production year on its way through. They are read first
    and written back afterwards, in the phase that survives.
    """
    url, _ = server
    client = client_for(url, dry_run=False)
    before = fetch(client, FIRST)
    assert before["PremiereDate"] and before["ProductionYear"]

    report = apply_identity(
        client, FIRST,
        {"Name": "The Quiet Harbour", "ProviderIds": {"Tmdb": "1001"}},
    )
    after = fetch(client, FIRST)

    assert after["PremiereDate"] == before["PremiereDate"]
    assert after["ProductionYear"] == before["ProductionYear"]
    assert any("restored after the apply" in note for note in report.notes)


def test_applying_an_identity_leaves_the_images_alone_by_default(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    apply_identity(client, FIRST, {"Name": "The Quiet Harbour"})
    route = [r for r in recorder.requests
             if r[0] == "POST" and "RemoteSearch" in r[1]]
    assert route, "the apply was sent"
    assert recorder.applied[0][0] == FIRST


def test_a_dry_run_identity_apply_says_what_it_would_restore(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    report = apply_identity(client_for(url), FIRST, {"Name": "The Quiet Harbour"})
    assert recorder.applied == []
    assert any("would restore" in note for note in report.notes)


def test_the_query_turns_image_replacement_off(
    server: tuple[str, Recorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch defaults to on at the other end, so it is named every time."""
    url, _ = server
    client = client_for(url, dry_run=False)
    seen: list[str] = []
    original = client.request

    def record(method: str, route: str, **kwargs: Any) -> Any:
        if "RemoteSearch" in route:
            seen.append(str(kwargs.get("params")))
        return original(method, route, **kwargs)

    monkeypatch.setattr(client, "request", record)
    apply_identity(client, FIRST, {"Name": "The Quiet Harbour"})
    assert "replaceAllImages" in seen[0]
    assert "False" in seen[0]
