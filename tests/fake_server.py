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
  moved data directory looks like from the outside;
* a scheduled-task list, so a maintenance pass can be made to refuse while
  the server is busy, and the two routes that start and stop one;
* a plugin list and one plugin's configuration, so scoping a pass can be
  tested without any plugin being installed anywhere;
* an ``IsMissing`` filter on the collection route, answered from rows marked
  ``LocationType`` ``Virtual``: episodes known only from a provider;
* a user list with more than one user in it, and a switch that makes it fail,
  so a play-state check that consults nobody has somebody to miss;
* the difference between the two ways a row can leave the catalogue. Deleting
  an item removes its **containing folder from disk** -- the file, its
  sidecars, and anything else that shares the folder -- which is what the
  real route does. A path notification saying a file was deleted drops only
  the row whose file is actually gone, and touches nothing on disk.

Every identifier here is the all-zero fixture shape and every title is from
the invented cast.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

USER_ID = "00000000-0000-0000-0000-000000000001"
SECOND_USER_ID = "00000000-0000-0000-0000-000000000002"
THIRD_USER_ID = "00000000-0000-0000-0000-000000000003"
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
    #: item id -> the extras the special-features route answers with
    special_features: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: every user-data body that was sent, as (user, item, body)
    user_data_writes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    #: item ids whose user-data writes are refused, to interrupt a replay
    user_data_refused: set[str] = field(default_factory=set)
    #: every collection query, as (user, lower-cased parameters)
    queries: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    #: what the plugin list answers with, and each plugin's configuration
    installed_plugins: list[dict[str, Any]] = field(default_factory=list)
    plugin_configuration: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: item id -> the marked stretches the server holds for it
    media_segments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: task ids that were started and cancelled, in order
    tasks_started: list[str] = field(default_factory=list)
    tasks_cancelled: list[str] = field(default_factory=list)
    #: what the scheduled-task routes answer with
    scheduled_tasks: list[dict[str, Any]] = field(default_factory=list)
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
    #: what the user list answers with, and a status that makes it fail instead
    users: list[dict[str, Any]] = field(default_factory=lambda: [
        {"Id": USER_ID, "Name": "first-fixture-user"},
        {"Id": SECOND_USER_ID, "Name": "second-fixture-user"},
        {"Id": THIRD_USER_ID, "Name": "third-fixture-user"},
    ])
    users_status: int | None = None
    #: a status to answer the library list with instead of the list
    virtual_folders_status: int | None = None
    #: folders an item delete removed from disk, as the real route does
    folders_deleted: list[str] = field(default_factory=list)
    #: rows a deleted-path notification dropped because their file was gone
    removed_by_scan: list[str] = field(default_factory=list)
    #: called with every batch of path notifications, after they are recorded:
    #: a test's stand-in for the scan the real server runs on the named folders
    on_notify: Callable[[list[dict[str, Any]]], None] | None = None
    #: an item delete only removes folders below this, so a fixture path can
    #: never reach anything outside the test's own temporary tree
    disk_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()).resolve()
    )

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

        if route == "/Plugins":
            self._send(200, self.recorder.installed_plugins)
            return
        if route.startswith("/Plugins/") and route.endswith("/Configuration"):
            plugin_id = route.split("/")[2]
            self._send(200, self.recorder.plugin_configuration.get(plugin_id, {}))
            return
        if route.startswith("/MediaSegments/"):
            item_id = route.rsplit("/", 1)[-1]
            self._send(200, {"Items": self.recorder.media_segments.get(item_id, [])})
            return
        if route == "/ScheduledTasks":
            self._send(200, self.recorder.scheduled_tasks)
            return
        if route == "/Library/VirtualFolders":
            if self.recorder.virtual_folders_status is not None:
                self._send(self.recorder.virtual_folders_status, {"error": "not now"})
            else:
                self._send(200, self.recorder.virtual_folders)
            return
        if route == "/Sessions":
            self._send(200, self.recorder.sessions)
            return
        if route == "/Users":
            if self.recorder.users_status is not None:
                self._send(self.recorder.users_status, {"error": "not now"})
            else:
                self._send(200, self.recorder.users)
            return
        if route == "/flaky":
            self.recorder.flaky_calls += 1
            if self.recorder.flaky_calls <= self.recorder.flaky_failures:
                self._send(503, {"error": "not now"})
            else:
                self._send(200, {"ok": True, "calls": self.recorder.flaky_calls})
            return
        if route.startswith("/Users/") and route.endswith("/Items") \
                and route.count("/") == 3:
            user = route.split("/")[2]
            if not any(row.get("Id") == user for row in self.recorder.users):
                self._send(404, {"error": "no such user"})
                return
            self._send(200, self._page(self._query(user, query), query))
            return
        if route.startswith("/Items/") and route.endswith("/SpecialFeatures"):
            item_id = route.split("/")[2]
            special_user = query.get("userId", [None])[0]
            if special_user is None:
                self._send(400, {"error": "this route needs a user"})
                return
            self._send(200, [self._with_user_data(row, special_user) for row in
                             self.recorder.special_features.get(item_id, [])])
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
        if route.startswith("/ScheduledTasks/Running/"):
            self.recorder.tasks_started.append(route.rsplit("/", 1)[-1])
            self._send(204)
            return
        if route.startswith("/Plugins/") and route.endswith("/Configuration"):
            plugin_id = route.split("/")[2]
            self.recorder.plugin_configuration[plugin_id] = dict(body or {})
            self._send(204)
            return
        if route == "/Library/VirtualFolders/LibraryOptions":
            self._write_library_options(body or {})
            return
        if route == "/Library/Media/Updated":
            updates = list((body or {}).get("Updates") or [])
            self.recorder.notifications += updates
            for update in updates:
                if update.get("UpdateType") == "Deleted":
                    self._scan_deleted(str(update.get("Path") or ""))
            if self.recorder.on_notify is not None:
                self.recorder.on_notify(updates)
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
            if item_id in self.recorder.user_data_refused:
                self._send(500, {"error": "the write was refused"})
                return
            key = (user, item_id)
            found = self.recorder.find(item_id)
            base = self.recorder.user_data.get(key) or dict(
                (found or {}).get("UserData") or {})
            # a field that is absent or null is left as it was, which is why a
            # last-played date can be set this way but never cleared
            base.update({k: v for k, v in (body or {}).items() if v is not None})
            self.recorder.user_data[key] = base
            self.recorder.user_data_writes.append((user, item_id, dict(body or {})))
            self._send(200, base)
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
        if route.startswith("/ScheduledTasks/Running/"):
            self.recorder.tasks_cancelled.append(route.rsplit("/", 1)[-1])
            self._send(204)
            return
        if route.startswith("/Items/"):
            item_id = route.rsplit("/", 1)[-1]
            found = self.recorder.find(item_id)
            if found is None:
                self._send(404, {"error": "no such item"})
                return
            self.recorder.items.remove(found)
            self.recorder.deleted.append(item_id)
            self._delete_containing_folder(str(found.get("Path") or ""))
            self._send(204)
            return
        self._send(404, {"error": "no such route"})

    # ---------------------------------------------------------- the two removals
    def _delete_containing_folder(self, raw: str) -> None:
        """What deleting an item does on the real server: the whole folder goes.

        Not the file -- the folder the file is in, with every sidecar, poster,
        extra and neighbouring film. Done for real below the test's temporary
        tree, so a test that reaches this route sees the collateral loss
        rather than a line in a list.
        """
        if not raw:
            return
        path = Path(raw)
        folder = path if path.is_dir() else path.parent
        self.recorder.folders_deleted.append(str(folder))
        try:
            inside = folder.resolve().is_relative_to(self.recorder.disk_root)
        except OSError:
            inside = False
        if inside and folder.resolve() != self.recorder.disk_root and folder.exists():
            shutil.rmtree(folder)

    def _scan_deleted(self, raw: str) -> None:
        """A deleted-path notification: the scan drops rows whose file is gone.

        A row whose file is still there stays, and nothing on disk is touched,
        which is the whole difference from deleting the item.
        """
        if not raw:
            return
        gone = PurePathLike(raw)
        for item in list(self.recorder.items):
            path = str(item.get("Path") or "")
            if path and PurePathLike(path).within(gone) and not Path(path).exists():
                self.recorder.items.remove(item)
                self.recorder.removed_by_scan.append(str(item["Id"]))

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
        self._send(200, self._with_user_data(found, user))

    def _with_user_data(self, row: dict[str, Any], user: str) -> dict[str, Any]:
        payload = copy.deepcopy(row)
        override = self.recorder.user_data.get((user, str(row.get("Id"))))
        if override is not None:
            payload["UserData"] = dict(override)
        return payload

    def _query(self, user: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        """The collection route's filters, as far as the library uses them.

        Parameter names are matched without regard to case, as the real
        server does. Without a parent the query covers everything, which is
        what a recursive query from the top answers with.
        """
        lowered = {key.lower(): values[0] for key, values in query.items() if values}
        rows = list(self.recorder.items)
        if "ids" in lowered:
            wanted = lowered["ids"].split(",")
            rows = [row for row in rows if row["Id"] in wanted]
        if "parentid" in lowered:
            parent = lowered["parentid"]
            recursive = lowered.get("recursive", "false").lower() == "true"
            rows = [row for row in rows if self._under(row, parent, recursive)]
        if "includeitemtypes" in lowered:
            kinds = lowered["includeitemtypes"].split(",")
            rows = [row for row in rows if row.get("Type") in kinds]
        if "mindatelastsaved" in lowered:
            # the stand-in's dates are all written in one format, so they
            # compare as text
            since = lowered["mindatelastsaved"]
            rows = [row for row in rows if str(row.get("DateLastSaved") or "") >= since]
        if "ismissing" in lowered:
            # a row is missing when the server knows it only from its provider
            wanted = lowered["ismissing"].lower() == "true"
            rows = [row for row in rows
                    if (row.get("LocationType") == "Virtual") == wanted]
        if "searchterm" in lowered:
            term = lowered["searchterm"].casefold()
            rows = [row for row in rows if term in str(row.get("Name", "")).casefold()]
        self.recorder.queries.append((user, dict(lowered)))
        return [self._with_user_data(row, user) for row in rows]

    def _under(self, row: dict[str, Any], parent: str, recursive: bool) -> bool:
        seen = 0
        here = row.get("ParentId")
        while here is not None and seen < 32:
            if here == parent:
                return True
            if not recursive:
                return False
            found = self.recorder.find(here)
            here = None if found is None else found.get("ParentId")
            seen += 1
        return False

    @staticmethod
    def _page(rows: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        start = int(query.get("startIndex", ["0"])[0])
        limit = int(query.get("limit", ["500"])[0])
        return {"Items": rows[start:start + limit], "TotalRecordCount": len(rows)}


def _unplayed() -> dict[str, Any]:
    return {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False,
            "IsFavorite": False}


def series_tree(
    series: str, seasons: dict[int, int], *, first: int = 100,
    root: str = "/srv/media/series",
) -> list[dict[str, Any]]:
    """A series row, its season rows and their episode rows, linked by parent.

    Identifiers count up from ``first`` in the all-zero fixture shape. The
    episodes carry the fields a slot is made of -- series, season number,
    episode number -- and each has a path in its season folder.
    """
    def ident(n: int) -> str:
        return f"00000000-0000-0000-0000-{n:012d}"

    number = first
    series_id = ident(number)
    rows: list[dict[str, Any]] = [{
        "Id": series_id, "Name": series, "Type": "Series", "ParentId": None,
        "Path": f"{root}/{series}", "ProviderIds": {"Tvdb": str(number)},
        "UserData": _unplayed(),
    }]
    for season, episodes in sorted(seasons.items()):
        number += 1
        season_id = ident(number)
        folder = f"{root}/{series}/Season {season:02d}"
        rows.append({
            "Id": season_id, "Name": f"Season {season}", "Type": "Season",
            "ParentId": series_id, "SeriesId": series_id, "IndexNumber": season,
            "Path": folder, "UserData": _unplayed(),
        })
        for episode in range(1, episodes + 1):
            number += 1
            rows.append({
                "Id": ident(number), "Name": f"Episode {episode}", "Type": "Episode",
                "ParentId": season_id, "SeasonId": season_id, "SeriesId": series_id,
                "SeriesName": series, "ParentIndexNumber": season,
                "IndexNumber": episode,
                "Path": f"{folder}/{series} - S{season:02d}E{episode:02d}.mkv",
                "ProviderIds": {"Tvdb": str(10_000 + number)},
                "UserData": _unplayed(),
            })
    return rows


class PurePathLike:
    """A path compared the way the server compares one: spelling, not the disk."""

    def __init__(self, raw: str) -> None:
        self.key = Path(raw).as_posix().casefold().rstrip("/")

    def within(self, other: PurePathLike) -> bool:
        return self.key == other.key or self.key.startswith(other.key + "/")


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


#: How often the serving loop checks whether it has been asked to stop.
SHUTDOWN_POLL_S = 0.005


@contextmanager
def fake_server() -> Iterator[tuple[str, Recorder]]:
    """Run the stand-in on a loopback port; yield its URL and its recorder."""
    recorder = Recorder()
    handler = type("BoundHandler", (_Handler,), {"recorder": recorder})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # ``shutdown`` waits for the serving loop to notice, and the loop only
    # looks between polls: at the default half second, every test that used
    # the stand-in paid half a second to stop it. Requests are answered as
    # they arrive whatever the interval; it only bounds how long stopping takes.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_S}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
