"""A stand-in media server, served on a loopback port for the duration of a test.

Never a recording of a real server, and never a real server. It implements the
routes the library uses, plus the failure modes that are worth testing
precisely because they are the ones that mislead:

* the unscoped single-item route refuses the request outright;
* the unscoped collection route *answers* -- with fewer items than exist, and
  no indication that anything is missing;
* a route that is briefly unavailable and then is not;
* a refresh that returns immediately and whose effect shows up three reads
  later, which is what makes "read it once afterwards" a broken check;
* an update route that keeps some of the fields it is sent and drops others
  on the floor, so the table of which is which has something to be tested
  against;
* a list-of-libraries route that answers with an identifier of nothing and
  options of nothing when the stored paths do not match, which is what a
  moved data directory looks like from the outside.

Every identifier here is the all-zero fixture shape and every title is from
the invented cast.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

USER_ID = "00000000-0000-0000-0000-000000000001"
SECOND_USER_ID = "00000000-0000-0000-0000-000000000002"
FIXTURE_CREDENTIAL = "a-fixture-value-not-a-real-token"

TICKS_PER_SECOND = 10_000_000


def _streams() -> list[dict[str, Any]]:
    return [
        {"Type": "Video", "Index": 0, "Codec": "h264", "Language": None},
        {"Type": "Audio", "Index": 1, "Codec": "ac3", "Language": "eng",
         "Channels": 6, "IsDefault": True, "IsForced": False, "Title": None},
        {"Type": "Audio", "Index": 2, "Codec": "ac3", "Language": "deu",
         "Channels": 2, "IsDefault": False, "IsForced": False, "Title": None},
    ]


def _chapters(count: int = 4) -> list[dict[str, Any]]:
    return [
        {"StartPositionTicks": index * 600 * TICKS_PER_SECOND,
         "Name": f"Chapter {index + 1}"}
        for index in range(count)
    ]


def _item(number: int, name: str, series: str | None = None) -> dict[str, Any]:
    kind = "Episode" if series else "Movie"
    folder = "series" if series else "movies"
    return {
        "Id": f"00000000-0000-0000-0000-{number:012d}",
        "Name": name,
        "Type": kind,
        "SeriesName": series,
        "IndexNumber": number if series else None,
        "ParentIndexNumber": 1 if series else None,
        "Overview": f"An invented description of {name}.",
        "ProviderIds": {"Tmdb": f"{1000 + number}"},
        "PremiereDate": "1978-04-03T22:00:00.0000000Z",
        "ProductionYear": 1978,
        "RunTimeTicks": 5400 * TICKS_PER_SECOND,
        "Chapters": _chapters(),
        "MediaStreams": _streams(),
        "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False,
                     "IsFavorite": False},
        "Trickplay": {"0": {"width": 320}},
        "Path": f"/srv/media/{folder}/{name}",
    }


#: Eight items, of which the unscoped collection route returns only five.
ITEMS: list[dict[str, Any]] = [
    _item(1, "The Quiet Harbour"),
    _item(2, "Blue Canyon"),
    _item(3, "Winter Tide"),
    _item(4, "Hollow Lantern"),
    _item(5, "Golden Meridian"),
    _item(6, "Signal Hill", series="Signal Hill"),
    _item(7, "Northwind", series="Northwind"),
    _item(8, "Harbour Lights", series="Harbour Lights"),
]
UNSCOPED_OMITS = 3

#: What the update route keeps out of a body it is sent, and what it drops.
#: The real thing assigns a fixed list of fields and ignores the rest; this
#: reproduces that shape so the published table has something to be wrong
#: against.
UPDATE_COPIES = frozenset({
    "Name", "OriginalTitle", "Overview", "Genres", "Tags", "Studios", "People",
    "IndexNumber", "ParentIndexNumber", "PremiereDate", "ProductionYear",
    "OfficialRating", "CustomRating", "CommunityRating", "ProviderIds",
    "LockedFields", "LockData", "RunTimeTicks", "Taglines", "ForcedSortName",
})
UPDATE_DISCARDS = frozenset({
    "Path", "ExtraType", "OwnerId", "ParentId", "SeasonId", "IndexNumberEnd",
    "Width", "Height", "ChannelNumber", "MediaStreams", "MediaSources",
    "UserData", "Id", "Type",
})


@dataclass
class Recorder:
    """What the server was asked to do, so a test can assert on the absence of it."""

    requests: list[tuple[str, str]] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    posted: list[tuple[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    applied: list[tuple[str, Any]] = field(default_factory=list)
    flaky_calls: int = 0
    #: how many times /flaky answers 503 before it answers
    flaky_failures: int = 2
    sessions: list[dict[str, Any]] = field(default_factory=list)
    #: the live records, so a test can mutate them and a POST can change them
    items: list[dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(ITEMS))
    #: play state per user, over and above what each record carries
    user_data: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: what the list-of-libraries route answers with
    virtual_folders: list[dict[str, Any]] = field(default_factory=list)
    #: library id -> where its options document lives, so a write can land
    options_documents: dict[str, Any] = field(default_factory=dict)
    #: every library-options body that was sent
    options_writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: item id -> how many more reads answer with the pre-refresh record
    stale_reads: dict[str, int] = field(default_factory=dict)
    #: what a refresh changes once it has caught up
    refresh_effect: dict[str, dict[str, Any]] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)

    def find(self, item_id: str) -> dict[str, Any] | None:
        for item in self.items:
            if item["Id"] == item_id:
                return item
        return None


class _Handler(BaseHTTPRequestHandler):
    recorder: Recorder

    def log_message(self, *_args: Any) -> None:
        return

    # ------------------------------------------------------------------ helpers
    def _send(self, status: int, payload: Any = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        return FIXTURE_CREDENTIAL in header

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ------------------------------------------------------------------- routes
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        self.recorder.requests.append(("GET", route))
        if not self._authorised():
            self._send(401, {"error": "no token"})
            return

        if route == "/Library/VirtualFolders":
            self._send(200, self.recorder.virtual_folders)
            return
        if route == "/Sessions":
            self._send(200, self.recorder.sessions)
            return
        if route == "/flaky":
            self.recorder.flaky_calls += 1
            if self.recorder.flaky_calls <= self.recorder.flaky_failures:
                self._send(503, {"error": "not now"})
            else:
                self._send(200, {"ok": True, "calls": self.recorder.flaky_calls})
            return
        if route == f"/Users/{USER_ID}/Items":
            self._send(200, self._page(self.recorder.items, query))
            return
        if route == "/Items":
            # answers, and quietly leaves items out: the failure worth testing
            self._send(200, self._page(self.recorder.items[:-UNSCOPED_OMITS], query))
            return
        if route.startswith("/Users/") and "/Items/" in route:
            user, _, item_id = route[len("/Users/"):].partition("/Items/")
            self._send_item(item_id, user)
            return
        if route.startswith("/Items/"):
            self._send(400, {"error": "this route needs a user"})
            return
        self._send(404, {"error": "no such route"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        self.recorder.requests.append(("POST", route))
        if not self._authorised():
            self._send(401, {"error": "no token"})
            return
        body = self._body()

        if route.endswith("/Refresh"):
            item_id = route.split("/")[2]
            self.recorder.refreshed.append(item_id)
            self._send(204)
            return
        if route == "/Library/VirtualFolders/LibraryOptions":
            self._write_library_options(body or {})
            return
        if route == "/Library/Media/Updated":
            self.recorder.notifications += list((body or {}).get("Updates") or [])
            self._send(204)
            return
        if route.startswith("/Items/RemoteSearch/Apply/"):
            item_id = route.rsplit("/", 1)[-1]
            self.recorder.applied.append((item_id, body))
            self._apply_identity(item_id, body or {})
            self._send(204)
            return
        if route.startswith("/UserItems/") and route.endswith("/UserData"):
            item_id = route.split("/")[2]
            user = parse_qs(parsed.query).get("userId", [USER_ID])[0]
            self.recorder.user_data[(user, item_id)] = dict(body or {})
            self._send(200, body)
            return
        if route.startswith("/Items/") and route.count("/") == 2:
            self._update_item(route.rsplit("/", 1)[-1], body or {})
            return
        self._send(404, {"error": "no such route"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        self.recorder.requests.append(("DELETE", route))
        if not self._authorised():
            self._send(401, {"error": "no token"})
            return
        if route.startswith("/Items/"):
            item_id = route.rsplit("/", 1)[-1]
            found = self.recorder.find(item_id)
            if found is None:
                self._send(404, {"error": "no such item"})
                return
            self.recorder.items.remove(found)
            self.recorder.deleted.append(item_id)
            self._send(204)
            return
        self._send(404, {"error": "no such route"})

    # ------------------------------------------------------------------ actions
    def _update_item(self, item_id: str, body: dict[str, Any]) -> None:
        found = self.recorder.find(item_id)
        if found is None:
            self._send(404, {"error": "no such item"})
            return
        if "Trickplay" in body:
            # the real thing fails here, and says nothing useful about why
            self._send(500, {"error": "unhandled exception"})
            return
        self.recorder.posted.append((item_id, copy.deepcopy(body)))
        for key in UPDATE_COPIES:
            # assigned unconditionally: a field left out of the body is not
            # left alone, which is the whole reason for the full round-trip
            found[key] = body.get(key)
        self._send(204)

    def _write_library_options(self, body: dict[str, Any]) -> None:
        library_id = str(body.get("Id") or "")
        options = dict(body.get("LibraryOptions") or {})
        self.recorder.options_writes.append((library_id, options))
        document = self.recorder.options_documents.get(library_id)
        if document is not None:
            write_options_document(document, options)
        self._send(204)

    def _apply_identity(self, item_id: str, body: dict[str, Any]) -> None:
        found = self.recorder.find(item_id)
        if found is None:
            return
        if body.get("Name"):
            found["Name"] = body["Name"]
        if body.get("ProviderIds"):
            found["ProviderIds"] = dict(body["ProviderIds"])
        # what the apply empties on the way through, every time
        found["PremiereDate"] = None
        found["ProductionYear"] = None

    def _send_item(self, item_id: str, user: str) -> None:
        found = self.recorder.find(item_id)
        if found is None:
            self._send(404, {"error": "no such item"})
            return
        self.recorder.reads.append(item_id)
        if item_id in self.recorder.refresh_effect and item_id in self.recorder.refreshed:
            remaining = self.recorder.stale_reads.get(item_id, 0)
            if remaining > 0:
                # the refresh was queued; this read still sees the old record
                self.recorder.stale_reads[item_id] = remaining - 1
            else:
                found.update(self.recorder.refresh_effect.pop(item_id))
        payload = copy.deepcopy(found)
        override = self.recorder.user_data.get((user, item_id))
        if override is not None:
            payload["UserData"] = dict(override)
        self._send(200, payload)

    @staticmethod
    def _page(rows: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        start = int(query.get("startIndex", ["0"])[0])
        limit = int(query.get("limit", ["500"])[0])
        return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}


def write_options_document(path: Any, values: dict[str, Any]) -> None:
    """Write an options document the way a server writes one back.

    Every field it was given, in a stable order. The interesting case -- a
    document that omits the fields still at their defaults -- is written by
    hand in the test that is about it.
    """
    from pathlib import Path as _Path

    lines = ["<LibraryOptions>"]
    for key in sorted(values):
        value = values[key]
        if isinstance(value, bool):
            lines.append(f"  <{key}>{'true' if value else 'false'}</{key}>")
        elif isinstance(value, int):
            lines.append(f"  <{key}>{value}</{key}>")
        elif isinstance(value, str):
            lines.append(f"  <{key}>{value}</{key}>")
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            inner = "".join(f"<string>{v}</string>" for v in value)
            lines.append(f"  <{key}>{inner}</{key}>")
    lines.append("</LibraryOptions>")
    text = "\n".join(lines) + "\n"
    _Path(path).write_text(text, encoding="utf-8", newline="\n")


#: The environment variable the fixture client resolves its credential from.
#: The value is a fixture string; the indirection is the point.
CREDENTIAL_VARIABLE = "JFKIT_FIXTURE_CREDENTIAL"


def client_for(url: str, **kwargs: Any) -> Any:
    """A client pointed at the stand-in, in dry-run mode unless told otherwise.

    Here rather than in each test module because eight of them want the same
    four lines, and a client built slightly differently in each one is how a
    test passes for a reason nobody intended.
    """
    from jfkit.client import Client, Retry
    from jfkit.config import Config, ServerConfig

    config = Config(
        server=ServerConfig(url=url, token_env=CREDENTIAL_VARIABLE, user_id=USER_ID)
    )
    defaults: dict[str, Any] = {
        "dry_run": True,
        "timeout_s": 10.0,
        "environ": {CREDENTIAL_VARIABLE: FIXTURE_CREDENTIAL},
        "retry": Retry(attempts=3, backoff_s=0.01),
    }
    defaults.update(kwargs)
    return Client(config, **defaults)


@contextmanager
def fake_server() -> Iterator[tuple[str, Recorder]]:
    """Run the stand-in on a loopback port; yield its URL and its recorder."""
    recorder = Recorder()
    handler = type("BoundHandler", (_Handler,), {"recorder": recorder})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
