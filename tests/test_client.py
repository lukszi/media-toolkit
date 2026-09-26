"""The client, against a stand-in server on a loopback port.

Never against a real one. A test that needs somebody's server is a test that
runs on one machine, and a client tested only against the server it was
written for is a client that encodes that server's accidents.

The tests worth reading here are the ones about the two routes that differ:
the unscoped single-item route refuses, and the unscoped collection route
answers short. Testing the second is the only way to keep a default honest.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
from jfkit.client import Client, Retry
from jfkit.config import Config, ConfigError, ServerConfig
from jfkit.errors import ItemNotFound, ServerRefused

from tests.fake_server import ITEMS, UNSCOPED_OMITS, USER_ID, Recorder, fake_server

CREDENTIAL_VARIABLE = "JFKIT_FIXTURE_CREDENTIAL"


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def make_client(url: str, **kwargs: object) -> Client:
    from tests.fake_server import FIXTURE_CREDENTIAL

    config = Config(
        server=ServerConfig(url=url, token_env=CREDENTIAL_VARIABLE, user_id=USER_ID)
    )
    defaults: dict[str, object] = {
        "dry_run": True,
        "timeout_s": 10.0,
        "environ": {CREDENTIAL_VARIABLE: FIXTURE_CREDENTIAL},
        "retry": Retry(attempts=3, backoff_s=0.01),
    }
    defaults.update(kwargs)
    return Client(config, **defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the basics
def test_a_client_needs_a_way_to_get_a_token() -> None:
    """A configuration with no indirection is refused when the client is built."""
    with pytest.raises(ConfigError, match="token_env or token_command"):
        Client(Config(server=ServerConfig(url="http://127.0.0.1:8096")))


def test_the_token_becomes_unloggable_before_it_becomes_usable(
    server: tuple[str, Recorder]
) -> None:
    """Resolving the token registers it with the redaction filter.

    The ordering is the guarantee: there is no window in which the value
    exists and the filter does not know about it, so no later mistake -- an
    exception that prints a header, a debug line somebody added at two in the
    morning -- can put it in a log file.
    """
    from mkvkit.logging import configure_logging

    from tests.fake_server import FIXTURE_CREDENTIAL

    url, _ = server
    client = make_client(url)
    written = io.StringIO()
    configure_logging(logging.INFO, stream=written)
    client.item(ITEMS[0]["Id"])
    logging.getLogger("jfkit.test").info("the header carries %s", client.token)
    logging.shutdown()
    text = written.getvalue()
    assert FIXTURE_CREDENTIAL not in text
    assert "***" in text


def test_a_request_without_a_token_is_refused_by_the_server(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    config = Config(
        server=ServerConfig(url=url, token_env=CREDENTIAL_VARIABLE, user_id=USER_ID)
    )
    client = Client(config, environ={CREDENTIAL_VARIABLE: "wrong"},
                    retry=Retry(attempts=1))
    with pytest.raises(ServerRefused) as caught:
        client.get("/Sessions")
    assert caught.value.status == 401


# ------------------------------------------------------- the scoping behaviour
def test_items_are_user_scoped_by_default(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    rows = list(make_client(url).items())
    assert len(rows) == len(ITEMS)
    assert all(route.startswith(f"/Users/{USER_ID}") for _method, route in recorder.requests)


def test_the_unscoped_collection_route_answers_short_and_says_nothing(
    server: tuple[str, Recorder], caplog: pytest.LogCaptureFixture
) -> None:
    """The reason the default is what it is, stated as a test.

    The short answer is not an error and carries no marker. Anything built on
    it under-reports silently, which is why asking for it has to be explicit
    and why it warns.
    """
    url, _ = server
    with caplog.at_level(logging.WARNING):
        rows = list(make_client(url).items(user_scoped=False))
    assert len(rows) == len(ITEMS) - UNSCOPED_OMITS
    assert "omit items" in caplog.text


def test_the_unscoped_single_item_route_refuses(server: tuple[str, Recorder]) -> None:
    url, _ = server
    client = make_client(url)
    with pytest.raises(ServerRefused) as caught:
        client.item(ITEMS[0]["Id"], user_scoped=False)
    assert caught.value.status == 400


def test_a_missing_item_is_a_lookup_error_not_a_server_error(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    with pytest.raises(ItemNotFound):
        make_client(url).item("00000000-0000-0000-0000-000000000099")


def test_items_without_a_user_id_configured_is_refused_before_the_request() -> None:
    config = Config(
        server=ServerConfig(url="http://127.0.0.1:8096", token_env=CREDENTIAL_VARIABLE)
    )
    client = Client(config, environ={CREDENTIAL_VARIABLE: "value"})
    with pytest.raises(ValueError, match="user_id"):
        list(client.items())


def test_paging_walks_the_whole_collection(server: tuple[str, Recorder]) -> None:
    url, _ = server
    rows = list(make_client(url).items(page_size=3))
    assert [row["Id"] for row in rows] == [item["Id"] for item in ITEMS]


# --------------------------------------------------------------- dry run first
def test_a_write_is_logged_and_not_sent(
    server: tuple[str, Recorder], caplog: pytest.LogCaptureFixture
) -> None:
    url, recorder = server
    client = make_client(url)
    with caplog.at_level(logging.INFO):
        assert client.post(f"/Items/{ITEMS[0]['Id']}/Refresh") is None
    assert recorder.refreshed == []
    assert "dry run" in caplog.text
    assert "Refresh" in caplog.text



def test_a_dry_run_write_logs_the_body_in_full_with_credentials_hidden(
    server: tuple[str, Recorder], caplog: pytest.LogCaptureFixture
) -> None:
    """The dry run is an account of what would be sent, so the body is in it."""
    url, recorder = server
    client = make_client(url)
    body = {
        "Name": "Blue Canyon",
        "Tags": ["one", "two"],
        "Nested": {"ApiKey": "a-fixture-key-value", "Keep": 3},
        "Password": "a-fixture-password-value",
    }
    with caplog.at_level(logging.INFO):
        client.post("/Plugins/00000000-0000-0000-0000-000000000301/Configuration", body)
    assert recorder.requests == []
    assert '"Name": "Blue Canyon"' in caplog.text
    assert '"Tags": ["one", "two"]' in caplog.text
    assert '"Keep": 3' in caplog.text
    assert "a-fixture-key-value" not in caplog.text
    assert "a-fixture-password-value" not in caplog.text
    assert caplog.text.count("<redacted>") == 2


def test_a_write_with_apply_is_sent(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    client = make_client(url, dry_run=False)
    client.post(f"/Items/{ITEMS[0]['Id']}/Refresh")
    assert recorder.refreshed == [ITEMS[0]["Id"]]


def test_reading_is_never_gated_by_the_dry_run_flag(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    assert make_client(url).item(ITEMS[1]["Id"])["Name"] == ITEMS[1]["Name"]


# --------------------------------------------------------------------- retries
def test_a_temporary_failure_is_retried(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    recorder.flaky_failures = 2
    assert make_client(url).get("/flaky")["ok"] is True
    assert recorder.flaky_calls == 3


def test_retrying_gives_up_and_reports_the_status(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    recorder.flaky_failures = 99
    with pytest.raises(ServerRefused) as caught:
        make_client(url).get("/flaky")
    assert caught.value.status == 503
    assert recorder.flaky_calls == 3


@pytest.mark.parametrize("status", [404, 503])
def test_every_refused_response_is_closed(status: int) -> None:
    """A refusal carries the open response; each one is closed, retried or not.

    Left to the garbage collector it holds a connection until it runs, and
    newer interpreters say so with a warning on every refused request.
    """
    import urllib.error
    from email.message import Message

    bodies: list[io.BytesIO] = []

    def refusing(request: object, timeout: float) -> object:
        body = io.BytesIO(b"no")
        bodies.append(body)
        raise urllib.error.HTTPError("http://127.0.0.1/x", status, "no", Message(), body)

    client = make_client("http://127.0.0.1:8096", opener=refusing)
    with pytest.raises(ServerRefused) as caught:
        client.get("/x")
    assert caught.value.status == status
    assert len(bodies) == (3 if status == 503 else 1)
    assert all(body.closed for body in bodies)


def test_a_write_is_never_retried(server: tuple[str, Recorder]) -> None:
    """One refresh that fails is better than three that half-succeeded."""
    url, recorder = server
    client = make_client(url, dry_run=False)
    with pytest.raises(ServerRefused):
        client.post("/no-such-route")
    assert len([r for r in recorder.requests if r[0] == "POST"]) == 1


# -------------------------------------------------------------------- sessions
def test_wait_idle_returns_at_once_when_nobody_is_watching(
    server: tuple[str, Recorder]
) -> None:
    url, _ = server
    make_client(url).wait_idle(timeout_s=1.0, poll_s=0.01)


def test_wait_idle_waits_for_playback_to_stop(server: tuple[str, Recorder]) -> None:
    url, recorder = server
    recorder.sessions = [
        {"NowPlayingItem": {"Id": ITEMS[0]["Id"]}, "PlayState": {"IsPaused": False}}
    ]
    waits: list[float] = []

    def stop_after_one(seconds: float) -> None:
        waits.append(seconds)
        recorder.sessions = []

    make_client(url).wait_idle(timeout_s=30.0, poll_s=5.0, sleep=stop_after_one)
    assert waits == [5.0]


def test_wait_idle_gives_up_rather_than_waiting_for_ever(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    recorder.sessions = [
        {"NowPlayingItem": {"Id": ITEMS[0]["Id"]}, "PlayState": {"IsPaused": False}}
    ]
    with pytest.raises(TimeoutError, match="still playing"):
        make_client(url).wait_idle(timeout_s=0.0, poll_s=0.0, sleep=lambda _s: None)


def test_a_paused_session_does_not_count_as_playing(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    recorder.sessions = [
        {"NowPlayingItem": {"Id": ITEMS[0]["Id"]}, "PlayState": {"IsPaused": True}},
        {"NowPlayingItem": None},
    ]
    make_client(url).wait_idle(timeout_s=1.0, poll_s=0.01)
