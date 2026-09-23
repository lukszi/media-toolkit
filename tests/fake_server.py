"""A stand-in media server, served on a loopback port for the duration of a test.

Never a recording of a real server, and never a real server. It implements the
handful of routes the client uses, plus the three failure modes that are worth
testing precisely because they are the ones that mislead:

* the unscoped single-item route refuses the request outright;
* the unscoped collection route *answers* -- with fewer items than exist, and
  no indication that anything is missing;
* a route that is briefly unavailable and then is not.

Every identifier here is the all-zero fixture shape and every title is from
the invented cast.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

USER_ID = "00000000-0000-0000-0000-000000000001"
FIXTURE_CREDENTIAL = "a-fixture-value-not-a-real-token"


def _item(number: int, name: str, series: str | None = None) -> dict[str, Any]:
    return {
        "Id": f"00000000-0000-0000-0000-{number:012d}",
        "Name": name,
        "Type": "Episode" if series else "Movie",
        "SeriesName": series,
        "IndexNumber": number if series else None,
        "Path": f"/srv/media/{'series' if series else 'movies'}/{name}",
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


@dataclass
class Recorder:
    """What the server was asked to do, so a test can assert on the absence of it."""

    requests: list[tuple[str, str]] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    flaky_calls: int = 0
    #: how many times /flaky answers 503 before it answers
    flaky_failures: int = 2
    sessions: list[dict[str, Any]] = field(default_factory=list)


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

    # ------------------------------------------------------------------- routes
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route, query = parsed.path, parse_qs(parsed.query)
        self.recorder.requests.append(("GET", route))
        if not self._authorised():
            self._send(401, {"error": "no token"})
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
            self._send(200, self._page(ITEMS, query))
            return
        if route == "/Items":
            # answers, and quietly leaves items out: the failure worth testing
            self._send(200, self._page(ITEMS[:-UNSCOPED_OMITS], query))
            return
        if route.startswith(f"/Users/{USER_ID}/Items/"):
            self._send_item(route.rsplit("/", 1)[-1])
            return
        if route.startswith("/Items/"):
            self._send(400, {"error": "this route needs a user"})
            return
        self._send(404, {"error": "no such route"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self.recorder.requests.append(("POST", parsed.path))
        if not self._authorised():
            self._send(401, {"error": "no token"})
            return
        if parsed.path.endswith("/Refresh"):
            self.recorder.refreshed.append(parsed.path.split("/")[2])
            self._send(204)
            return
        self._send(404, {"error": "no such route"})

    def _send_item(self, item_id: str) -> None:
        for item in ITEMS:
            if item["Id"] == item_id:
                self._send(200, item)
                return
        self._send(404, {"error": "no such item"})

    @staticmethod
    def _page(rows: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        start = int(query.get("startIndex", ["0"])[0])
        limit = int(query.get("limit", ["500"])[0])
        return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}


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
