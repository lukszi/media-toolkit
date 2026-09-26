"""jfkit.userdata -- carry every user's watched state across an identifier change.

A rename, a move or a renumber gives an item a new identifier, and play state
is stored against the identifier: the new item starts unwatched for everybody.
This module takes a snapshot of every user's state first, replays it onto the
new identifiers afterwards through a mapping, and verifies the result.

**The state of a row.** Five fields, per user and item: ``Played``,
``PlayCount``, ``PlaybackPositionTicks``, ``LastPlayedDate`` and
``IsFavorite`` (:class:`UserState`). A row is *blank* when it is unplayed,
never played, at position zero and not a favourite; the last-played date does
not count, for the reason below.

**Rows the server hands out by slot.** After a renumber the server can give
a *new* episode the play state that was recorded for its season/episode
slot -- the state of whichever file used to carry that number. A replay that
only writes the snapshot onto the new identifiers leaves those rows behind:
an episode nobody has seen shows as watched twice. So a replay does not only
write; it reads every item in the scope afterwards (the scope defaults to the
mapped items, and is normally widened to the whole series) and **clears every
row the snapshot does not account for**. That is what :func:`expectations`
computes: for each (item, user), the state the snapshot justifies -- the
mapped source's row, or blank for an item that no snapshot row maps onto.

**What cannot be cleared.** The user-data route leaves any field it is not
sent, or is sent as null, as it was. A last-played date can therefore be set
but never removed through it. A cleared row keeps the date it inherited, and
:func:`verify` lists those rows separately instead of calling them a failure
or pretending they are clean.

**How it runs.** The replay is a :class:`mkvkit.steps.Plan` of
``userdata.write`` steps: printed by the dry run, applied with a JSON-lines
audit, resumable after a partial failure. Reads are user-scoped and run
side by side within a limit (``workers``, default
:data:`mkvkit.lanes.DEFAULT_WORKERS`); every write is its own step.

Anything that changes play state while a rename is in progress -- somebody
watching -- is overwritten by the replay. Take the snapshot, rename, replay,
in one sitting, with :meth:`jfkit.client.Client.wait_idle` before it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mkvkit.steps import Action, FunctionAction, Plan, Step

from .client import Client
from .query import FIELDS, playstate

__all__ = [
    "SCHEMA",
    "Mismatch",
    "Snapshot",
    "SnapshotItem",
    "UserState",
    "VerifyReport",
    "actions",
    "expectations",
    "load_mapping",
    "plan_replay",
    "snapshot",
    "verify",
]

log = logging.getLogger(__name__)

#: The version of the snapshot document.
SCHEMA = 1

#: The action name of a replay step.
WRITE = "userdata.write"


def _instant(text: str | None) -> str | None:
    """A server date reduced to the second, so two spellings of it compare equal.

    The server writes seven fractional digits and a ``Z``; a date read back
    through another route, or typed by hand, may carry fewer or an offset.
    """
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    main, _, rest = raw.partition(".")
    offset = ""
    for sign in ("+", "-"):
        if sign in rest:
            offset = sign + rest.split(sign, 1)[1]
            break
    try:
        moment = datetime.fromisoformat(main + offset)
    except ValueError:
        return text
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class UserState:
    """One user's state for one item."""

    played: bool = False
    play_count: int = 0
    position_ticks: int = 0
    last_played: str | None = None
    favorite: bool = False

    @classmethod
    def from_server(cls, data: Mapping[str, Any] | None) -> UserState:
        data = data or {}
        return cls(
            played=bool(data.get("Played")),
            play_count=int(data.get("PlayCount") or 0),
            position_ticks=int(data.get("PlaybackPositionTicks") or 0),
            last_played=data.get("LastPlayedDate") or None,
            favorite=bool(data.get("IsFavorite")),
        )

    @property
    def blank(self) -> bool:
        """Unplayed, never played, at the start and not a favourite."""
        return (not self.played and self.play_count == 0
                and self.position_ticks == 0 and not self.favorite)

    def same_as(self, other: UserState) -> bool:
        """Equal in every field the route can set, and in the date if one is expected."""
        core = (self.played, self.play_count, self.position_ticks, self.favorite)
        theirs = (other.played, other.play_count, other.position_ticks, other.favorite)
        if core != theirs:
            return False
        return self.last_played is None or _instant(self.last_played) == _instant(
            other.last_played)

    def body(self) -> dict[str, Any]:
        """The user-data route's body; the date only when there is one to set."""
        out: dict[str, Any] = {
            "Played": self.played, "PlayCount": self.play_count,
            "PlaybackPositionTicks": self.position_ticks, "IsFavorite": self.favorite,
        }
        if self.last_played:
            out["LastPlayedDate"] = self.last_played
        return out

    def to_json(self) -> dict[str, Any]:
        return {"Played": self.played, "PlayCount": self.play_count,
                "PlaybackPositionTicks": self.position_ticks,
                "LastPlayedDate": self.last_played, "IsFavorite": self.favorite}

    def __str__(self) -> str:
        if self.blank:
            return "blank" + (f" (last played {self.last_played})" if self.last_played else "")
        parts = ["played" if self.played else "unplayed", f"count {self.play_count}"]
        if self.position_ticks:
            parts.append(f"at {self.position_ticks / 10_000_000:.0f}s")
        if self.favorite:
            parts.append("favourite")
        return ", ".join(parts)


@dataclass(frozen=True)
class SnapshotItem:
    """One item as it was when the snapshot was taken, with every user's row."""

    id: str
    name: str | None = None
    type: str | None = None
    path: str | None = None
    series: str | None = None
    season: int | None = None
    episode: int | None = None
    states: Mapping[str, UserState] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "Id": self.id, "Name": self.name, "Type": self.type, "Path": self.path,
            "SeriesName": self.series, "ParentIndexNumber": self.season,
            "IndexNumber": self.episode,
            "UserData": {user: state.to_json() for user, state in sorted(self.states.items())},
        }


@dataclass(frozen=True)
class Snapshot:
    """Every user's state for a set of items, at one moment."""

    users: tuple[str, ...]
    items: tuple[SnapshotItem, ...]
    taken: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def item(self, item_id: str) -> SnapshotItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        document = {"schema": SCHEMA, "taken": self.taken, "users": list(self.users),
                    "items": [item.to_json() for item in self.items]}
        out.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8", newline="\n")
        return out

    @classmethod
    def load(cls, path: Path | str) -> Snapshot:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema") != SCHEMA:
            raise ValueError(f"{path}: snapshot schema {raw.get('schema')!r}, "
                             f"expected {SCHEMA}")
        items = tuple(
            SnapshotItem(
                id=str(row["Id"]), name=row.get("Name"), type=row.get("Type"),
                path=row.get("Path"), series=row.get("SeriesName"),
                season=row.get("ParentIndexNumber"), episode=row.get("IndexNumber"),
                states={user: UserState.from_server(data)
                        for user, data in (row.get("UserData") or {}).items()},
            )
            for row in raw.get("items") or []
        )
        return cls(users=tuple(raw.get("users") or ()), items=items,
                   taken=str(raw.get("taken", "")))


# ----------------------------------------------------------------- snapshot
def snapshot(
    client: Client,
    item_ids: Iterable[str],
    *,
    user_ids: Sequence[str] | None = None,
    workers: int | None = None,
) -> Snapshot:
    """Read every user's state for every item; refuse to return half of it.

    A snapshot with a hole in it is worse than none -- the replay would clear
    the rows it did not see -- so a request that fails, or an item no user's
    query returned, raises instead of producing a partial document.
    """
    wanted = list(dict.fromkeys(item_ids))
    report = playstate(client, wanted, user_ids=user_ids, workers=workers)
    if report.errors:
        user, batch, error = report.errors[0]
        raise RuntimeError(
            f"{len(report.errors)} request(s) failed, the first for user {user} "
            f"({len(batch)} item(s)): {error}; no snapshot was written"
        )
    if report.missing:
        raise LookupError(f"not found on the server: {', '.join(report.missing)}")
    who = tuple(dict.fromkeys(row["UserId"] for row in report.rows))
    records = {str(item["Id"]): item for item in _records(client, wanted)}
    items = []
    for item_id in wanted:
        record = records.get(item_id, {})
        states = {
            row["UserId"]: UserState.from_server(row)
            for row in report.rows if row["ItemId"] == item_id
        }
        items.append(SnapshotItem(
            id=item_id, name=record.get("Name"), type=record.get("Type"),
            path=record.get("Path"), series=record.get("SeriesName"),
            season=record.get("ParentIndexNumber"), episode=record.get("IndexNumber"),
            states=states,
        ))
    return Snapshot(users=who, items=tuple(items))


def _records(client: Client, ids: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for start in range(0, len(ids), 50):
        out += list(client.items(Ids=",".join(ids[start:start + 50]), Fields=FIELDS))
    return out


# ------------------------------------------------------------------ mapping
def load_mapping(path: Path | str) -> dict[str, str]:
    """Old identifier -> new identifier, from JSON (an object) or two TSV columns.

    Lines starting with ``#`` and a header line naming ``old``/``new`` are
    ignored in the tab-separated form.
    """
    text = Path(path).read_text(encoding="utf-8-sig")
    if text.lstrip().startswith("{"):
        raw = json.loads(text)
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for row in csv.reader(text.splitlines(), delimiter="\t"):
        if not row or row[0].startswith("#") or row[0].strip().lower() == "old":
            continue
        if len(row) < 2:
            raise ValueError(f"{path}: a mapping line needs two columns: {row!r}")
        out[row[0].strip()] = row[1].strip()
    return out


def _check_mapping(mapping: Mapping[str, str]) -> None:
    targets: dict[str, str] = {}
    for old, new in mapping.items():
        if new in targets:
            raise ValueError(
                f"{targets[new]} and {old} both map onto {new}: whose state should it get?"
            )
        targets[new] = old


# -------------------------------------------------------------- expectations
def expectations(
    snapshot_: Snapshot,
    mapping: Mapping[str, str],
    scope: Iterable[str] = (),
    user_ids: Iterable[str] = (),
) -> dict[tuple[str, str], UserState]:
    """The state every (item, user) should have after the replay.

    Each snapshot item lands on ``mapping.get(old, old)`` -- an item the
    mapping does not mention kept its identifier. Every other item in
    ``scope`` should be blank: nothing in the snapshot justifies state there,
    so whatever it has was handed out by slot. Users are the snapshot's plus
    ``user_ids``; a user the snapshot has no row for is expected blank.
    """
    _check_mapping(mapping)
    who = list(dict.fromkeys([*snapshot_.users, *user_ids]))
    expected: dict[tuple[str, str], UserState] = {}
    for item in snapshot_.items:
        target = mapping.get(item.id, item.id)
        for user in who:
            expected[(target, user)] = item.states.get(user, UserState())
    for item_id in scope:
        for user in who:
            expected.setdefault((item_id, user), UserState())
    return expected


@dataclass(frozen=True)
class Mismatch:
    item: str
    user: str
    expected: UserState
    actual: UserState
    #: set on a row whose only difference is a last-played date the route cannot clear
    date_only: bool = False

    def __str__(self) -> str:
        if self.date_only:
            return (f"{self.item} for {self.user}: cleared, but it keeps the last-played "
                    f"date {self.actual.last_played}, which the server cannot remove")
        return f"{self.item} for {self.user}: expected {self.expected}, found {self.actual}"


def _compare(
    client: Client,
    expected: Mapping[tuple[str, str], UserState],
    *,
    workers: int | None,
) -> tuple[list[Mismatch], list[str]]:
    items = list(dict.fromkeys(item for item, _user in expected))
    who = list(dict.fromkeys(user for _item, user in expected))
    report = playstate(client, items, user_ids=who, workers=workers)
    problems = [f"user {u}: {len(b)} item(s) unread: {e}" for u, b, e in report.errors]
    problems += [f"{item}: not found on the server" for item in report.missing]
    unread = {i for _u, b, _e in report.errors for i in b} | set(report.missing)
    mismatches: list[Mismatch] = []
    for (item, user), want in expected.items():
        if item in unread:
            continue
        row = report.state(item, user)
        have = UserState.from_server(row)
        if want.same_as(have):
            if want.blank and want.last_played is None and have.last_played:
                mismatches.append(Mismatch(item, user, want, have, date_only=True))
            continue
        mismatches.append(Mismatch(item, user, want, have))
    return mismatches, problems


# -------------------------------------------------------------------- replay
def plan_replay(
    client: Client,
    snapshot_: Snapshot,
    mapping: Mapping[str, str],
    *,
    scope: Iterable[str] = (),
    user_ids: Sequence[str] | None = None,
    workers: int | None = None,
) -> Plan:
    """The writes that bring every (item, user) in scope to its expected state.

    Reads the current state first, so a row that is already right costs no
    write, and a row the snapshot does not justify gets a step that clears it.
    Refuses (raises) when an item in scope cannot be read: a replay that did
    not see a row cannot say whether it needs clearing.
    """
    extra_users = list(user_ids) if user_ids is not None else list(_all_users(client))
    expected = expectations(snapshot_, mapping, scope, extra_users)
    mismatches, problems = _compare(client, expected, workers=workers)
    if problems:
        raise RuntimeError("the current state could not be read in full: "
                           + "; ".join(problems[:5]))
    steps: list[Step] = []
    cleared = 0
    dated = 0
    for number, found in enumerate(
        (m for m in mismatches if not m.date_only), start=1
    ):
        clearing = found.expected.blank
        cleared += clearing
        dated += bool(clearing and found.actual.last_played)
        verb = "clear" if clearing else "write"
        steps.append(Step(
            id=f"userdata:{number:04d}", action=WRITE,
            params={"item": found.item, "user": found.user, "body": found.expected.body(),
                    "was": found.actual.to_json()},
            summary=f"{verb} {found.item} for user {found.user}: {found.actual} -> "
                    f"{found.expected}",
        ))
    notes = [f"{len(expected)} (item, user) row(s) in scope, {len(steps)} to write"]
    if cleared:
        notes.append(
            f"{cleared} row(s) hold state the snapshot does not account for -- "
            "handed out by season/episode slot, or recorded since -- and are cleared"
        )
    dates = sum(1 for m in mismatches if m.date_only) + dated
    if dates:
        notes.append(f"{dates} cleared row(s) keep a last-played date: the server "
                     "cannot remove one")
    return Plan(verb="userdata replay", steps=tuple(steps), notes=tuple(notes))


def _all_users(client: Client) -> list[str]:
    from .query import users

    return list(users(client))


def actions(client: Client) -> dict[str, Action]:
    """The step executors a replay plan needs, bound to one client."""
    def write(step: Step) -> Mapping[str, Any]:
        item, user = step.params["item"], step.params["user"]
        client.post(f"/UserItems/{item}/UserData", step.params["body"], userId=user)
        return {"item": item, "user": user}

    def done(step: Step) -> bool:
        item, user = step.params["item"], step.params["user"]
        report = playstate(client, [item], user_ids=[user], workers=1)
        row = report.state(item, user)
        return row is not None and UserState.from_server(step.params["body"]).same_as(
            UserState.from_server(row))

    return {WRITE: FunctionAction(write, done)}


# -------------------------------------------------------------------- verify
@dataclass(frozen=True)
class VerifyReport:
    """What differs from the expected state after a replay."""

    rows: int
    mismatches: tuple[Mismatch, ...] = ()
    #: blank rows that kept a last-played date: reported, not failed
    dates_left: tuple[Mismatch, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.problems

    def __str__(self) -> str:
        lines = [
            f"{self.rows} (item, user) row(s) checked: {len(self.mismatches)} differ, "
            f"{len(self.dates_left)} keep a last-played date the server cannot clear, "
            f"{len(self.problems)} could not be read"
        ]
        lines += [f"  DIFFERS {m}" for m in self.mismatches]
        lines += [f"  note: {m}" for m in self.dates_left]
        lines += [f"  UNREAD {p}" for p in self.problems]
        return "\n".join(lines)


def verify(
    client: Client,
    snapshot_: Snapshot,
    mapping: Mapping[str, str],
    *,
    scope: Iterable[str] = (),
    user_ids: Sequence[str] | None = None,
    workers: int | None = None,
) -> VerifyReport:
    """Read everything in scope again and compare it with :func:`expectations`."""
    extra_users = list(user_ids) if user_ids is not None else _all_users(client)
    expected = expectations(snapshot_, mapping, scope, extra_users)
    mismatches, problems = _compare(client, expected, workers=workers)
    return VerifyReport(
        rows=len(expected),
        mismatches=tuple(m for m in mismatches if not m.date_only),
        dates_left=tuple(m for m in mismatches if m.date_only),
        problems=tuple(problems),
    )

