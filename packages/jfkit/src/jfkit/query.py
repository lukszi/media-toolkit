"""jfkit.query -- find items, list their children, read everybody's play state.

The read side a cleanup pass keeps needing and ``item show`` cannot give,
because ``item show`` wants an identifier up front: which item is this
series, what are the episodes of this season, which extras does this film
have, and who has watched what.

**Every query is user-scoped.** The unscoped collection route answers with
fewer items than exist and says nothing about it (see :mod:`jfkit.client`), so
nothing here uses it. Play state is read *as each user*, through that user's
own collection route, because it is stored per user and the administrator's
view of an item carries only the administrator's.

**Requests run side by side, within a limit.** Reading play state for a few
hundred items and eight users is a few dozen requests once the identifiers
are batched; they are sent through :func:`mkvkit.lanes.map_bounded` with
``workers`` at a time (:data:`mkvkit.lanes.DEFAULT_WORKERS` by default). A
batch that fails is reported with its error and does not lose the others.

**Output is rows.** Every function returns plain dictionaries with fixed
keys, and :func:`render` turns a list of them into JSON or tab-separated text,
so the verbs pipe into whatever reads them next.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mkvkit.lanes import map_bounded

from .client import Client

__all__ = [
    "BATCH",
    "ITEM_COLUMNS",
    "PLAYSTATE_COLUMNS",
    "USER_DATA_FIELDS",
    "PlayStateReport",
    "children",
    "find",
    "item_row",
    "playstate",
    "render",
    "users",
]

log = logging.getLogger(__name__)

#: The play-state fields a user's row carries, in the server's spelling.
USER_DATA_FIELDS: tuple[str, ...] = (
    "Played", "PlayCount", "PlaybackPositionTicks", "LastPlayedDate", "IsFavorite",
)

#: Identifiers asked for in one request. Long enough to keep the request
#: count low, short enough to stay far below any URL length limit.
BATCH = 50

#: The extra fields every query asks for, beyond what a row always carries.
FIELDS = "Path,ProviderIds,ParentId"

#: The columns of an item row, in order.
ITEM_COLUMNS: tuple[str, ...] = (
    "Id", "Type", "Name", "SeriesName", "ParentIndexNumber", "IndexNumber",
    "ParentId", "Path", "ProviderIds",
)

#: The columns of a play-state row, in order.
PLAYSTATE_COLUMNS: tuple[str, ...] = (
    "ItemId", "Name", "UserId", "UserName", *USER_DATA_FIELDS,
)


def item_row(item: Mapping[str, Any]) -> dict[str, Any]:
    """One item reduced to :data:`ITEM_COLUMNS`; provider ids as ``Key=value;...``."""
    providers = item.get("ProviderIds") or {}
    row = {column: item.get(column) for column in ITEM_COLUMNS}
    row["ProviderIds"] = ";".join(f"{k}={v}" for k, v in sorted(providers.items()) if v)
    return row


def users(client: Client) -> dict[str, str]:
    """Every user the server has: identifier -> name."""
    found = client.get("/Users")
    rows = found if isinstance(found, list) else []
    return {
        str(row["Id"]): str(row.get("Name", ""))
        for row in rows if isinstance(row, dict) and row.get("Id")
    }


def _normalised(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _provider_matches(item: Mapping[str, Any], wanted: str) -> bool:
    providers = {str(k).casefold(): str(v) for k, v in (item.get("ProviderIds") or {}).items()}
    key, sep, value = wanted.partition("=")
    if sep:
        return providers.get(key.casefold()) == value
    return wanted in providers.values()


def find(
    client: Client,
    *,
    name: str | None = None,
    exact: bool = False,
    path: str | None = None,
    provider: str | None = None,
    types: Sequence[str] = (),
    parent: str | None = None,
) -> list[dict[str, Any]]:
    """Items matching every criterion given, as full records.

    ``name`` is a case-insensitive substring (or the whole name with
    ``exact``); the server narrows by it first. ``path`` is a glob against the
    item's path, compared with either separator and without regard to case --
    a pattern without a wildcard matches that path and everything under it.
    ``provider`` is ``Key=value`` (``Tmdb=1001``) or a bare value matched
    against any provider. ``types`` are server item types (``Series``,
    ``Season``, ``Episode``, ``Movie``, ...). ``parent`` limits the search to
    one item's descendants.
    """
    params: dict[str, Any] = {"Recursive": True, "Fields": FIELDS}
    if name:
        params["SearchTerm"] = name
    if types:
        params["IncludeItemTypes"] = ",".join(types)
    if parent:
        params["ParentId"] = parent
    pattern = None
    if path:
        pattern = _normalised(path)
        if not any(ch in pattern for ch in "*?["):
            pattern = pattern.rstrip("/")
    out: list[dict[str, Any]] = []
    for item in client.items(**params):
        if name:
            here = str(item.get("Name", "")).casefold()
            if (here != name.casefold()) if exact else (name.casefold() not in here):
                continue
        if pattern is not None:
            where = _normalised(str(item.get("Path") or ""))
            if "*" in pattern or "?" in pattern or "[" in pattern:
                if not fnmatch.fnmatchcase(where, pattern):
                    continue
            elif not (where == pattern or where.startswith(pattern + "/")):
                continue
        if provider and not _provider_matches(item, provider):
            continue
        out.append(item)
    return out


def children(
    client: Client,
    item_id: str,
    *,
    recursive: bool = False,
    types: Sequence[str] = (),
    extras: bool = False,
) -> list[dict[str, Any]]:
    """An item's children -- a season's episodes, a series' seasons -- or its extras.

    ``recursive`` returns every descendant (all episodes of a series, with
    ``types=("Episode",)``). ``extras`` returns the special features instead
    -- trailers, featurettes, deleted scenes -- which are not children and do
    not appear in the ordinary listing at all.
    """
    if extras:
        found = client.get(f"/Items/{item_id}/SpecialFeatures",
                           userId=client._require_user())
        rows = found if isinstance(found, list) else (found or {}).get("Items") or []
        return [row for row in rows if isinstance(row, dict)]
    params: dict[str, Any] = {"ParentId": item_id, "Fields": FIELDS}
    if recursive:
        params["Recursive"] = True
    if types:
        params["IncludeItemTypes"] = ",".join(types)
    return list(client.items(**params))


@dataclass(frozen=True)
class PlayStateReport:
    """Play-state rows, and the requests that could not be answered."""

    rows: tuple[dict[str, Any], ...]
    #: (user id, the identifiers asked for, the error) per failed request
    errors: tuple[tuple[str, tuple[str, ...], str], ...] = ()
    #: identifiers no user's query returned
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing

    def state(self, item_id: str, user: str) -> dict[str, Any] | None:
        for row in self.rows:
            if row["ItemId"] == item_id and row["UserId"] == user:
                return row
        return None


def playstate(
    client: Client,
    item_ids: Iterable[str],
    *,
    user_ids: Sequence[str] | None = None,
    workers: int | None = None,
) -> PlayStateReport:
    """Every named user's play state for every item, one row per (item, user).

    With no ``user_ids`` every user on the server is read: a check that reads
    only the users somebody remembered to name passes over everybody else.
    Rows come back in the order of ``item_ids``, then of the users.
    """
    wanted = list(dict.fromkeys(item_ids))
    names = users(client)
    who = list(user_ids) if user_ids else list(names)
    if not who:
        raise ValueError("the server listed no users, so there is no play state to read")
    batches = [tuple(wanted[i:i + BATCH]) for i in range(0, len(wanted), BATCH)]
    jobs = [(user, batch) for user in who for batch in batches]

    def read(job: tuple[str, tuple[str, ...]]) -> list[dict[str, Any]]:
        user, batch = job
        return list(client.items(user=user, Ids=",".join(batch), Fields=FIELDS,
                                 EnableUserData=True))

    outcomes = map_bounded(read, jobs, workers=workers)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[tuple[str, tuple[str, ...], str]] = []
    seen: set[str] = set()
    for outcome in outcomes:
        user, batch = outcome.item
        if not outcome.ok:
            errors.append((user, batch, f"{type(outcome.error).__name__}: {outcome.error}"))
            continue
        for item in outcome.result or []:
            item_id = str(item.get("Id"))
            seen.add(item_id)
            data = item.get("UserData") or {}
            row: dict[str, Any] = {
                "ItemId": item_id, "Name": item.get("Name"),
                "UserId": user, "UserName": names.get(user, ""),
            }
            row.update({field: data.get(field) for field in USER_DATA_FIELDS})
            found[(item_id, user)] = row
    rows = [found[(i, u)] for i in wanted for u in who if (i, u) in found]
    failed_ids = {i for _u, batch, _e in errors for i in batch}
    missing = tuple(i for i in wanted if i not in seen and i not in failed_ids)
    return PlayStateReport(rows=tuple(rows), errors=tuple(errors), missing=missing)


def render(
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    fmt: Literal["json", "tsv"] = "tsv",
) -> str:
    """Rows as a JSON list or as tab-separated text with a header line."""
    if fmt == "json":
        return json.dumps([{c: row.get(c) for c in columns} for row in rows],
                          ensure_ascii=False, indent=1)

    def cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value).replace("\t", " ").replace("\n", " ")

    lines = ["\t".join(columns)]
    lines += ["\t".join(cell(row.get(c)) for c in columns) for row in rows]
    return "\n".join(lines)
