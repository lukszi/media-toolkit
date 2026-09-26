"""jfkit.safedelete.catalogue -- every path the server has a row for, read once.

A leftover is defined by what the catalogue does *not* hold, so the catalogue
is read whole, once, user-scoped, and indexed three ways: by the exact path
of each row, by every folder above a row (so "does anything catalogued live
at or below this folder" is one lookup), and by provider identifier (so a
leftover description file can be matched to the item it once described).

Only rows that stand for media count: films, episodes, series, seasons,
videos. A plain ``Folder`` row is the server's view of a directory, not an
item, and a leftover folder may well have one; it is not a reason to keep it.

Paths are compared by spelling, folded and with one separator, which is how
the server compares them. A path that differs only in case is the same path
here -- the cautious answer, since it can only add refusals.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..client import Client

__all__ = [
    "CATALOGUE_FIELDS",
    "MEDIA_TYPES",
    "Catalogue",
    "describe",
    "exists",
    "path_key",
]

log = logging.getLogger(__name__)

#: The row types that stand for media. ``Folder`` and ``CollectionFolder`` are
#: left out on purpose (see the module notes).
MEDIA_TYPES: tuple[str, ...] = (
    "Movie", "Episode", "Series", "Season", "Video", "MusicVideo", "Trailer",
)

#: What each row is read with: enough to name it, place it and match it.
CATALOGUE_FIELDS: tuple[str, ...] = (
    "Path", "ProviderIds", "ProductionYear", "SeriesName", "SeriesId", "SeasonId",
    "ParentIndexNumber", "IndexNumber", "IndexNumberEnd", "LocationType",
    "PremiereDate", "ParentId",
)


def path_key(path: str | os.PathLike[str]) -> str:
    """One spelling per path: forward slashes, folded case, no trailing slash."""
    text = os.fspath(path).replace("\\", "/").casefold()
    while len(text) > 1 and text.endswith("/") and not text.endswith(":/"):
        text = text[:-1]
    return text


def _ancestors(key: str) -> Iterable[str]:
    """The key itself and every folder above it."""
    yield key
    here = key
    while "/" in here.rstrip("/"):
        here = here.rsplit("/", 1)[0]
        if not here:
            break
        yield here + ("/" if here.endswith(":") else "")
        if here.endswith(":"):
            break


@dataclass(frozen=True)
class Catalogue:
    """The media rows the server has, indexed for leftover questions."""

    rows: tuple[dict[str, Any], ...]
    _at: Mapping[str, tuple[dict[str, Any], ...]] = field(repr=False)
    _below: Mapping[str, tuple[dict[str, Any], ...]] = field(repr=False)
    _provider: Mapping[tuple[str, str], tuple[dict[str, Any], ...]] = field(repr=False)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> Catalogue:
        kept = tuple(dict(row) for row in rows if row.get("Type") in MEDIA_TYPES)
        at: dict[str, list[dict[str, Any]]] = {}
        below: dict[str, list[dict[str, Any]]] = {}
        provider: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in kept:
            raw = row.get("Path")
            if raw:
                key = path_key(str(raw))
                at.setdefault(key, []).append(row)
                for ancestor in _ancestors(key):
                    below.setdefault(ancestor, []).append(row)
            for name, value in (row.get("ProviderIds") or {}).items():
                if value:
                    provider.setdefault((str(name).casefold(), str(value).casefold()),
                                        []).append(row)
        return cls(
            rows=kept,
            _at={k: tuple(v) for k, v in at.items()},
            _below={k: tuple(v) for k, v in below.items()},
            _provider={k: tuple(v) for k, v in provider.items()},
        )

    @classmethod
    def fetch(cls, client: Client, *, types: Sequence[str] = MEDIA_TYPES) -> Catalogue:
        """Every media row, user-scoped, in one paged query."""
        rows = list(client.items(
            recursive=True, includeItemTypes=",".join(types),
            fields=",".join(CATALOGUE_FIELDS),
        ))
        log.info("catalogue: %d media row(s)", len(rows))
        return cls.from_rows(rows)

    # ---------------------------------------------------------------- lookups
    def at(self, path: str | os.PathLike[str]) -> tuple[dict[str, Any], ...]:
        """The rows whose path is exactly this one."""
        return self._at.get(path_key(path), ())

    def at_or_below(self, folder: str | os.PathLike[str]) -> tuple[dict[str, Any], ...]:
        """The rows whose path is this folder or anything inside it."""
        return self._below.get(path_key(folder), ())

    def by_provider(self, name: str, value: str) -> tuple[dict[str, Any], ...]:
        return self._provider.get((name.casefold(), value.casefold()), ())

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("Type") in types]

    def copies_of(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Other rows that stand for the same film or episode.

        An episode is the same episode when series, season and number agree;
        a film when any provider identifier agrees. The row itself is never
        its own copy.
        """
        ident = row.get("Id")
        out: list[dict[str, Any]] = []
        if row.get("Type") == "Episode":
            for other in self.rows:
                if other.get("Id") == ident or other.get("Type") != "Episode":
                    continue
                if (other.get("SeriesId"), other.get("ParentIndexNumber"),
                        other.get("IndexNumber")) == (
                        row.get("SeriesId"), row.get("ParentIndexNumber"),
                        row.get("IndexNumber")) and row.get("SeriesId"):
                    out.append(other)
            return out
        seen: set[str] = set()
        for name, value in (row.get("ProviderIds") or {}).items():
            if not value:
                continue
            for other in self.by_provider(str(name), str(value)):
                other_id = str(other.get("Id"))
                if other_id != ident and other_id not in seen \
                        and other.get("Type") == row.get("Type"):
                    seen.add(other_id)
                    out.append(other)
        return out


def describe(row: Mapping[str, Any]) -> str:
    """One row as a person reads it: type, name, year or slot, and path."""
    kind = str(row.get("Type") or "item")
    name = str(row.get("Name") or "?")
    if kind == "Episode":
        slot = ""
        if row.get("ParentIndexNumber") is not None and row.get("IndexNumber") is not None:
            slot = f" S{int(row['ParentIndexNumber']):02d}E{int(row['IndexNumber']):02d}"
        head = f"{kind} {row.get('SeriesName') or '?'}{slot} {name!r}"
    else:
        year = f" ({row['ProductionYear']})" if row.get("ProductionYear") else ""
        head = f"{kind} {name!r}{year}"
    where = row.get("Path")
    return f"{head} at {where}" if where else f"{head}, no path"


def exists(path: str | os.PathLike[str] | None) -> bool:
    return bool(path) and Path(os.fspath(path or "")).exists()
