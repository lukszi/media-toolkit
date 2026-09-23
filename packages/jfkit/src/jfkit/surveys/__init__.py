"""jfkit.surveys -- read-only inventories of a library, in one shape.

Six questions worth being able to answer about a collection without opening
anything: how complete the metadata is, what languages the audio tracks claim,
what state the chapter marks are in, what containers and codecs are actually
in use, what is in there twice, and how the filenames will be read the next
time anything is scanned.

Everything here reads. Nothing in this package has a write path at all, which
is why none of it takes a dry-run switch: there is nothing to switch off.

Three rules hold across all six.

**User-scoped queries, always.** The unscoped collection route answers with
fewer items than exist and says nothing about the difference. A survey built
on it under-reports silently, which is the worst possible failure for a
document whose entire purpose is to be believed later.

**The survey is built from records, not from a connection.** Every builder
takes an iterable of item records, so every one of them is testable against a
handful of hand-written ones, and a survey can be re-run over a saved fetch
without asking the server again.

**The caveats are part of the survey.** Each builder attaches the ones that
apply to it -- what it counted, what it could not see, and which of its
numbers are derived from the catalogue rather than from the files.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..client import Client
from ..report import Survey

__all__ = [
    "BUILDERS",
    "DEFAULT_FIELDS",
    "Builder",
    "build",
    "fetch_items",
    "names",
]

log = logging.getLogger(__name__)

#: What a builder is: records in, one survey out.
Builder = Callable[[Sequence[Mapping[str, Any]]], Survey]

#: The fields every survey here needs. Asked for once, in one query, because
#: the expensive part of a library-wide question is the number of round trips
#: and not the size of each answer.
DEFAULT_FIELDS: tuple[str, ...] = (
    "Path", "ProviderIds", "Overview", "PremiereDate", "ProductionYear", "Genres",
    "People", "Studios", "ImageTags", "BackdropImageTags", "CommunityRating",
    "OfficialRating", "RunTimeTicks", "MediaStreams", "MediaSources", "Chapters",
    "SeriesName", "SeriesId", "IndexNumber", "ParentIndexNumber", "LockedFields",
    "DateCreated", "Container",
)

#: The types a library-wide survey asks about by default.
DEFAULT_TYPES: tuple[str, ...] = ("Movie", "Series", "Season", "Episode", "BoxSet")


def fetch_items(
    client: Client,
    *,
    types: Sequence[str] = DEFAULT_TYPES,
    fields: Sequence[str] = DEFAULT_FIELDS,
    **params: Any,
) -> list[dict[str, Any]]:
    """Every item of the named types, user-scoped, with the fields a survey needs.

    User-scoped is not a preference here. The unscoped route returns a short
    answer rather than an error, so a survey built on it is quietly wrong and
    stays that way until somebody notices an item missing from a report months
    later.
    """
    rows = list(
        client.items(
            recursive=True,
            includeItemTypes=",".join(types),
            fields=",".join(fields),
            **params,
        )
    )
    log.info("fetched %d item(s) of %s", len(rows), ", ".join(types))
    return rows


def _registry() -> dict[str, Builder]:
    from .chapters import chapter_state
    from .completeness import completeness
    from .inventory import containers, duplicates, filename_parse
    from .languages import audio_languages

    return {
        "metadata": completeness,
        "audio-languages": audio_languages,
        "chapters": chapter_state,
        "containers": containers,
        "duplicates": duplicates,
        "filename-parse": filename_parse,
    }


#: Every survey, by the name the command line uses.
BUILDERS: Mapping[str, Builder] = _registry()


def names() -> list[str]:
    return sorted(BUILDERS)


def build(name: str, items: Iterable[Mapping[str, Any]]) -> Survey:
    """One survey by name, over records already fetched."""
    if name not in BUILDERS:
        raise ValueError(f"{name}: not one of {', '.join(names())}")
    return BUILDERS[name](list(items))
