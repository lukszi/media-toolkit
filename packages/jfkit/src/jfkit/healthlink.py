"""jfkit.healthlink -- the server's half of ``mkvkit health``.

The scan itself is file-side and lives in :mod:`mkvkit.health`; that package
does not depend on this one. Two things it offers only when this package is
installed, and it finds them here by name:

* **the gate** -- :func:`lane_gate` builds a :class:`jfkit.jobs.LaneGate`
  for its lanes, so a disk the server is playing from, generating previews
  on, or scanning is left alone until it is quiet;
* **the titles** -- :func:`with_titles` names every file that is not OK by
  the server item that plays it, through :func:`jfkit.query.find`, so a
  report reads as programmes and not as paths.

Both read. Neither writes anything, to the server or to the disk.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from mkvkit.health import OK, FileResult

from .client import Client
from .config import Config
from .devices import Device
from .jobs import Gate, LaneGate
from .query import find

__all__ = ["TITLE_TYPES", "lane_gate", "server_configured", "title_of", "with_titles"]

log = logging.getLogger(__name__)

#: The item types a video file can be played as.
TITLE_TYPES: tuple[str, ...] = ("Movie", "Episode", "Video", "MusicVideo")


class _Waiting(Protocol):
    def waiting(self, device: Device, why: str | None) -> None: ...


def server_configured(config: Config) -> bool:
    server = config.server
    return bool(server.url and (server.token_env or server.token_command))


def lane_gate(
    config: Config,
    *,
    server: bool = True,
    every_s: float = 60.0,
    timeout_s: float | None = None,
    locks: Sequence[Path | str] = (),
    progress: _Waiting | None = None,
) -> LaneGate:
    """The gate a scan's lanes wait on; with ``server``, it asks the server too."""
    client = Client(config, dry_run=True) if server and server_configured(config) else None
    if server and client is None:
        log.info("no server configured: the gate reads this machine only")

    def held(device: Device, found: Gate) -> None:
        why = ", ".join(found.red_by)
        detail = found.reasons[0] if found.reasons else ""
        if progress is not None:
            progress.waiting(device, f"RED by {why}: {detail}")
        else:
            log.info("%s", found)

    def clear(device: Device, _found: Gate) -> None:
        if progress is not None:
            progress.waiting(device, None)

    return LaneGate(
        client=client, every_s=every_s, timeout_s=timeout_s, locks=locks,
        max_readers=config.jobs.max_readers_per_device, on_hold=held, on_clear=clear,
    )


def _key(path: str) -> str:
    return path.replace("\\", "/").casefold()


def title_of(item: Mapping[str, Any]) -> str:
    """How a person would name the item: a film by its year, an episode by its slot."""
    name = str(item.get("Name") or "")
    if item.get("Type") == "Episode":
        series = item.get("SeriesName") or "?"
        season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
        slot = (
            f"S{int(season):02d}E{int(episode):02d}"
            if isinstance(season, int) and isinstance(episode, int) else "S?E?"
        )
        return f"{series} {slot} {name}".strip()
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year else name


def with_titles(
    config: Config,
    results: Iterable[FileResult],
    *,
    client: Client | None = None,
    include_ok: bool = False,
) -> list[FileResult]:
    """The results, each file that is not OK named by the item that plays it.

    One paged query for every playable item, matched by path, rather than one
    query per file. A file no item plays keeps no id: that is worth knowing
    too, and the report says so by leaving the column empty.
    """
    rows = list(results)
    wanted = [r for r in rows if include_ok or r.verdict != OK]
    if not wanted:
        return rows
    asking = client or Client(config, dry_run=True)
    index: dict[str, Mapping[str, Any]] = {}
    for row in find(asking, types=TITLE_TYPES):
        path = row.get("Path")
        if isinstance(path, str) and path:
            index.setdefault(_key(path), row)
    out: list[FileResult] = []
    for result in rows:
        named = include_ok or result.verdict != OK
        item = index.get(_key(result.path)) if named else None
        if item is None:
            out.append(result)
            continue
        out.append(replace(result, item_id=str(item.get("Id") or ""), title=title_of(item)))
    return out
