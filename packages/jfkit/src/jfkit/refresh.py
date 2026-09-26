"""jfkit.refresh -- ask for a refresh, wait for it, and check what else moved.

A folder of one-off scripts asked for a refresh in slightly different ways and
checked slightly different things afterwards. This is the one that survives, and
it is built around four findings that cost real time to learn.

**A refresh is a request, not an event.** The call returns at once with
nothing in it; the work is queued. A record read a minute later can still be
the record from before, and reading it once and believing it is how a batch
reports success over a queue that has not started. :func:`safe_refresh` polls
until the caller's own condition holds, or until it gives up and says so.

**Non-replacing, unless somebody insists.** A replacing refresh re-fetches
everything from the providers, which discards the hand corrections that were
the reason for the work. The default here does not replace, and the switch
that does is spelled out in full rather than hidden behind a truthy argument.

**A cleared name does not come back from a provider if a sidecar file is
sitting next to the media.** The local sidecar reader runs before any remote
provider, sees an empty name, and restores the one it already holds -- which
is the filename-derived string that was being removed in the first place.
:func:`nfo_guard` says so before the refresh rather than after it: set the
name, do not clear it and hope.

**Applying a remote identity empties the air date and the production year**,
and turns image replacement on unless it is turned off in the query. Both are
handled in :func:`apply_identity`, which snapshots those fields, applies the
identity, and writes them back in the phase that survives -- the last one.

The nudge is here too. A media server can be told that one path changed
instead of being asked to walk a whole library, which turns an hour into a
second. It has one rule, and breaking it is how an accidental full scan
starts: name the file, or the deepest folder that changed -- never a library
root.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from .client import Client
from .dto import Comparison, compare, fetch, update_item

__all__ = [
    "DATE_FIELDS",
    "NOTIFY_KINDS",
    "NotifyRefused",
    "RefreshReport",
    "apply_identity",
    "library_roots",
    "nfo_guard",
    "notify_changed",
    "request_refresh",
    "safe_refresh",
]

log = logging.getLogger(__name__)

#: The fields a remote identity apply empties on its way through.
DATE_FIELDS: tuple[str, ...] = ("PremiereDate", "ProductionYear")

#: What a path notification can say happened.
NOTIFY_KINDS = frozenset({"Created", "Modified", "Deleted"})

#: Sidecar extensions a local metadata reader will fill an empty field from.
SIDECAR_SUFFIXES: tuple[str, ...] = (".nfo",)


class NotifyRefused(ValueError):
    """A path notification was not sent, because sending it starts a full scan."""


@dataclass(frozen=True)
class RefreshReport:
    """What was asked for, how long it took, and what moved that should not have."""

    item_id: str
    requested: bool
    settled: bool
    polls: int
    waited_s: float
    comparison: Comparison | None = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Settled in time, and nothing outside the expected changes moved."""
        return self.settled and (self.comparison is None or self.comparison.ok)

    def __str__(self) -> str:
        state = "settled" if self.settled else "did not settle"
        lines = [
            f"{self.item_id}: {state} after {self.waited_s:.0f}s "
            f"({self.polls} poll(s))"
        ]
        if self.comparison is not None:
            lines += ["  " + line for line in str(self.comparison).splitlines()[1:]]
            if self.comparison.ok:
                lines.append("  nothing else moved")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


# ------------------------------------------------------------------ the call
def request_refresh(
    client: Client,
    item_id: str,
    *,
    metadata: str = "FullRefresh",
    images: str = "Default",
    replace_all_metadata: bool = False,
    replace_all_images: bool = False,
) -> None:
    """Queue a refresh for one item.

    The two replacement switches default to off and are named in full on
    purpose. Replacing metadata is how an afternoon of corrections goes back
    to whatever a provider thinks; replacing images is how a hand-chosen
    poster becomes a different one.
    """
    if replace_all_metadata:
        log.warning(
            "%s: refreshing with replacement on -- every field a provider has an "
            "opinion about will be overwritten, including the ones edited by hand",
            item_id,
        )
    client.post(
        f"/Items/{item_id}/Refresh",
        None,
        metadataRefreshMode=metadata,
        imageRefreshMode=images,
        replaceAllMetadata=replace_all_metadata,
        replaceAllImages=replace_all_images,
    )


def safe_refresh(
    client: Client,
    item_id: str,
    *,
    expected_changes: Iterable[str] = (),
    until: Callable[[Mapping[str, Any]], bool] | None = None,
    metadata: str = "FullRefresh",
    timeout_s: float = 300.0,
    poll_s: float = 10.0,
    sleep: Callable[[float], None] | None = None,
    before: Mapping[str, Any] | None = None,
) -> RefreshReport:
    """Snapshot, refresh without replacing, wait for it, and diff the result.

    ``until`` is the caller's own definition of "the queue got to it" -- the
    stream table has the right number of tracks, the name is no longer the
    filename, whatever the operation was for. Without one the first read after
    the refresh is taken as the answer, which is right only when nothing about
    the file itself changed.

    Everything outside ``expected_changes`` is compared and reported. A
    refresh that quietly rewrites a name somebody fixed by hand is the reason
    this function exists, and it is common enough to have its own test.
    """
    rest = sleep or time.sleep
    snapshot = dict(before) if before is not None else fetch(client, item_id)
    request_refresh(client, item_id, metadata=metadata)
    if client.dry_run:
        return RefreshReport(
            item_id=item_id, requested=False, settled=False, polls=0, waited_s=0.0,
            notes=("dry run: the refresh was not sent and nothing was waited for",),
        )

    started = time.monotonic()
    polls = 0
    current = snapshot
    settled = False
    while True:
        polls += 1
        current = fetch(client, item_id)
        if until is None or until(current):
            settled = True
            break
        waited = time.monotonic() - started
        if waited >= timeout_s:
            break
        log.info("%s: not settled after %.0fs, waiting %.0fs", item_id, waited, poll_s)
        rest(poll_s)

    waited = time.monotonic() - started
    notes: list[str] = []
    if not settled:
        notes.append(
            f"the queue had not caught up after {waited:.0f}s; the comparison below "
            "is against a record that may still be the old one"
        )
    return RefreshReport(
        item_id=item_id,
        requested=True,
        settled=settled,
        polls=polls,
        waited_s=waited,
        comparison=compare(snapshot, current, expected=expected_changes),
        notes=tuple(notes),
    )


# ----------------------------------------------------------------- the nudge
def library_roots(client: Client) -> list[str] | None:
    """Every folder of every library the server has, or None if it would not say.

    The roots a configuration names are the ones somebody remembered; a
    library added later, or one on another volume, is a root all the same,
    and notifying it is the same full scan. None means the list could not be
    read, which a caller must treat as "do not notify", never as "no roots".
    """
    try:
        found = client.get("/Library/VirtualFolders")
    except Exception:  # unreadable: the caller does not notify at all
        return None
    if not isinstance(found, list):
        return None
    roots: list[str] = []
    for library in found:
        if isinstance(library, dict):
            roots += [str(p) for p in library.get("Locations") or []]
    return roots


def notify_changed(
    client: Client,
    paths: Iterable[Path | str],
    *,
    roots: Iterable[Path | str] = (),
    kind: str = "Modified",
) -> list[str]:
    """Tell the server that these paths changed, and nothing wider than that.

    The rule that makes this safe is the only rule it has: a notification
    naming a library root is not a notification, it is a full library
    validation, and on a large collection that is hours of disk. So every
    configured root is refused outright, as is any ancestor of one.

    Name the file itself where you can. A folder is for the case where the
    change is the set of files in it.
    """
    if kind not in NOTIFY_KINDS:
        raise ValueError(f"{kind}: not one of {sorted(NOTIFY_KINDS)}")
    root_paths = [PurePath(str(r)) for r in roots]
    sent: list[str] = []
    updates: list[dict[str, str]] = []
    for raw in paths:
        candidate = PurePath(str(raw))
        for root in root_paths:
            if candidate == root or root.is_relative_to(candidate):
                raise NotifyRefused(
                    f"{candidate} is a library root, or contains one: notifying it "
                    "starts a full validation of everything below it. Name the file "
                    "that changed, or the deepest folder that did."
                )
        updates.append({"Path": str(candidate), "UpdateType": kind})
        sent.append(str(candidate))
    if updates:
        client.post("/Library/Media/Updated", {"Updates": updates})
    return sent


# --------------------------------------------------------------- the guards
def nfo_guard(
    item: Mapping[str, Any],
    *,
    exists: Callable[[Path], bool] | None = None,
) -> list[str]:
    """Say whether "clear the name and let a provider fill it" can work here.

    It cannot, wherever the server has written a sidecar next to the media:
    the local reader runs first, sees the empty field and restores its own
    title -- which is the bad name, because the server wrote that sidecar from
    the bad name. The fix is to set the name explicitly instead of clearing
    it, and this returns the sentence that says so.
    """
    path = item.get("Path")
    if not path:
        return []
    here = Path(path)
    is_file = exists or (lambda p: p.is_file())
    found = [
        here.with_suffix(suffix)
        for suffix in SIDECAR_SUFFIXES
        if is_file(here.with_suffix(suffix))
    ]
    if not found:
        return []
    return [
        f"{f.name} sits beside this item: clearing the name and refreshing will "
        "restore the name from it, not from a provider. Set the name instead."
        for f in found
    ]


def apply_identity(
    client: Client,
    item_id: str,
    candidate: Mapping[str, Any],
    *,
    replace_all_images: bool = False,
    restore: Sequence[str] = DATE_FIELDS,
    refresh: Callable[[str], None] | None = None,
) -> RefreshReport:
    """Apply a searched-for identity, and put back the fields it empties.

    Two things happen on the way through that nobody asks for. Image
    replacement is on unless the query says otherwise, so a hand-picked
    poster is replaced by whatever the provider has; that is turned off here
    by default. And the air date and production year are emptied, because an
    identity apply is a replacing operation -- so they are read first and
    written back afterwards, in the phase that survives a refresh, which is
    the last one.
    """
    before = fetch(client, item_id)
    keep = {name: before.get(name) for name in restore if before.get(name) is not None}

    client.post(
        f"/Items/RemoteSearch/Apply/{item_id}",
        dict(candidate),
        replaceAllImages=replace_all_images,
    )
    if client.dry_run:
        return RefreshReport(
            item_id=item_id, requested=False, settled=False, polls=0, waited_s=0.0,
            notes=(
                "dry run: the identity was not applied",
                f"would restore afterwards: {', '.join(sorted(keep)) or 'nothing'}",
            ),
        )

    after = fetch(client, item_id)
    emptied = {k: v for k, v in keep.items() if after.get(k) is None}
    notes: list[str] = []
    if emptied:
        update_item(client, item_id, emptied, refresh=refresh)
        notes.append(
            "restored after the apply emptied them: " + ", ".join(sorted(emptied))
        )
        after = fetch(client, item_id)
    return RefreshReport(
        item_id=item_id,
        requested=True,
        settled=True,
        polls=1,
        waited_s=0.0,
        comparison=compare(
            before, after, expected=("Name", "ProviderIds", "Overview", *restore)
        ),
        notes=tuple(notes),
    )
