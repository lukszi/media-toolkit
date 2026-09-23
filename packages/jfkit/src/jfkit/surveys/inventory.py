"""Three inventories: what containers are in use, what is in there twice, and
how the filenames will be read.

**Containers.** A census of container, video codec, audio codec and
resolution. It exists because the alternative -- assembling one by hand when
a question comes up -- produces a table with no generator, which cannot be
re-run and therefore cannot be compared with anything.

**Duplicates.** Grouped by provider identifier, which is the only grouping
that survives a rename. Two rows are reported where the same identifier
appears on more than one item, and the group carries enough to decide with:
each one's size, runtime, resolution, container and how many audio tracks it
has. It deliberately does not decide: a smaller file with an extra dubbed
track is not the worse copy, and the survey's job is to put the numbers side
by side.

**Filename parse.** Every episode path run through the predictor, so a
rename that would be read as an episode *range* is visible before anything is
renamed rather than after. This is the survey that pays for itself fastest,
because the outcome it predicts cannot be fixed through the API at all.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..dto import TICKS_PER_SECOND
from ..naming import PARSED_AGAINST, Rule, parse
from ..report import Column, Survey

__all__ = ["containers", "duplicates", "filename_parse"]


def _first_source(item: Mapping[str, Any]) -> Mapping[str, Any]:
    sources = item.get("MediaSources") or []
    return sources[0] if sources else {}


def _streams(item: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [s for s in (item.get("MediaStreams") or []) if s.get("Type") == kind]


def _resolution(item: Mapping[str, Any]) -> str:
    video = _streams(item, "Video")
    if not video:
        return ""
    width, height = video[0].get("Width"), video[0].get("Height")
    return f"{width}x{height}" if width and height else ""


def containers(items: Sequence[Mapping[str, Any]]) -> Survey:
    """One row per playable item: container, codecs, resolution, size."""
    columns = [
        Column("item", "Item"),
        Column("type", "Type"),
        Column("container", "Container"),
        Column("video", "Video codec"),
        Column("audio", "Audio codecs"),
        Column("resolution", "Resolution"),
        Column("audio_tracks", "Audio tracks", kind="number"),
        Column("subtitle_tracks", "Subtitle tracks", kind="number"),
        Column("size_gib", "Size (GiB)", kind="number", places=2),
    ]
    rows: list[dict[str, Any]] = []
    census: dict[tuple[str, str], int] = {}
    total_bytes = 0
    unknown_size = 0

    for item in items:
        if item.get("Type") not in {"Movie", "Episode"}:
            continue
        source = _first_source(item)
        container = str(
            source.get("Container") or item.get("Container") or ""
        ).split(",")[0].lower()
        video = _streams(item, "Video")
        audio = _streams(item, "Audio")
        subtitles = _streams(item, "Subtitle")
        video_codec = (video[0].get("Codec") or "").lower() if video else ""
        size = source.get("Size")
        if size is None:
            unknown_size += 1
        else:
            total_bytes += int(size)
        census[(container, video_codec)] = census.get((container, video_codec), 0) + 1
        rows.append({
            "item": item.get("Name"),
            "type": item.get("Type"),
            "container": container,
            "video": video_codec,
            "audio": ",".join(sorted({(s.get("Codec") or "").lower() for s in audio})),
            "resolution": _resolution(item),
            "audio_tracks": len(audio),
            "subtitle_tracks": len(subtitles),
            "size_gib": None if size is None else int(size) / 2**30,
        })

    summary: dict[str, Any] = {
        "playable items": len(rows),
        "total size": f"{total_bytes / 2**40:.2f} TiB",
        "items with no size recorded": unknown_size,
    }
    for (container, codec), count in sorted(
        census.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        summary[f"{container or '(unknown)'} / {codec or '(unknown)'}"] = count
    return Survey(
        name="Container census",
        about="What containers, codecs and resolutions are actually in use.",
        columns=columns,
        rows=rows,
        summary=summary,
        scope={"types": "Movie, Episode", "items seen": len(items)},
        caveats=[
            "Everything is read from the catalogue, which records what the server "
            "made of each file when it last looked at it. A file changed since "
            "then is described as it was.",
            "The container is the first one the source names. A file whose probe "
            "reports several is listed under the first, which is what a player "
            "would use.",
        ],
    )


def duplicates(items: Sequence[Mapping[str, Any]]) -> Survey:
    """Items sharing a provider identifier, with the numbers to choose between them.

    Grouped by identifier rather than by name, because a rename changes the
    name and not the identity, and two copies of one film are usually named
    differently -- that is generally why there are two.
    """
    columns = [
        Column("group", "Group"),
        Column("provider", "Provider"),
        Column("identifier", "Identifier"),
        Column("item", "Item"),
        Column("path", "Path"),
        Column("size_gib", "Size (GiB)", kind="number", places=2),
        Column("runtime_min", "Runtime (min)", kind="number", places=1),
        Column("resolution", "Resolution"),
        Column("container", "Container"),
        Column("audio_tracks", "Audio tracks", kind="number"),
        Column("subtitle_tracks", "Subtitle tracks", kind="number"),
    ]
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in items:
        if item.get("Type") not in {"Movie", "Episode"}:
            continue
        for provider, identifier in (item.get("ProviderIds") or {}).items():
            if identifier:
                groups.setdefault((str(provider), str(identifier)), []).append(item)

    rows: list[dict[str, Any]] = []
    reclaimable = 0
    number = 0
    for (provider, identifier), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        number += 1
        sizes = []
        for item in members:
            source = _first_source(item)
            size = source.get("Size")
            sizes.append(int(size) if size is not None else 0)
            runtime = item.get("RunTimeTicks")
            rows.append({
                "group": number,
                "provider": provider,
                "identifier": identifier,
                "item": item.get("Name"),
                "path": item.get("Path"),
                "size_gib": None if size is None else int(size) / 2**30,
                "runtime_min": (
                    None if not runtime else float(runtime) / TICKS_PER_SECOND / 60.0
                ),
                "resolution": _resolution(item),
                "container": str(
                    source.get("Container") or item.get("Container") or ""
                ).split(",")[0].lower(),
                "audio_tracks": len(_streams(item, "Audio")),
                "subtitle_tracks": len(_streams(item, "Subtitle")),
            })
        reclaimable += sum(sizes) - max(sizes) if sizes else 0

    return Survey(
        name="Duplicates by identity",
        about="Items that share a provider identifier, and the numbers to "
              "choose between them.",
        columns=columns,
        rows=rows,
        summary={
            "groups": number,
            "items in a group": len(rows),
            "at most reclaimable": f"{reclaimable / 2**30:.1f} GiB",
        },
        scope={"types": "Movie, Episode", "items seen": len(items)},
        caveats=[
            "Grouped by provider identifier. Two copies of one film with no "
            "identifier on either are not grouped, and a wrongly identified item "
            "is grouped with whatever it was identified as.",
            "The reclaimable figure assumes keeping the largest of each group, "
            "which is not a recommendation. A smaller file carrying a track the "
            "larger one does not have is not the worse copy.",
            "Nothing here is a decision. The evidence for deleting anything is "
            "gathered per item, against the file, and that is a different tool.",
        ],
    )


def filename_parse(items: Sequence[Mapping[str, Any]]) -> Survey:
    """How each episode path will be read, before anything is renamed."""
    columns = [
        Column("item", "Item"),
        Column("path", "Path"),
        Column("rule", "Read by"),
        Column("season", "Season", kind="number"),
        Column("episode", "Episode", kind="number"),
        Column("end", "Range ends at", kind="number"),
        Column("would_get_range", "Read as a range", kind="bool"),
        Column("matches_catalogue", "Agrees with the catalogue", kind="bool"),
    ]
    rows: list[dict[str, Any]] = []
    ranges = 0
    unclaimed = 0
    disagreements = 0
    for item in items:
        path = item.get("Path")
        if item.get("Type") != "Episode" or not path:
            continue
        found = parse(str(path))
        catalogued = (item.get("ParentIndexNumber"), item.get("IndexNumber"))
        agrees = (
            found.season is None or catalogued[0] is None
            or (found.season, found.episode) == catalogued
        )
        ranges += found.would_get_end
        unclaimed += found.rule is Rule.NONE
        disagreements += not agrees
        rows.append({
            "item": item.get("Name"),
            "path": path,
            "rule": found.rule.value,
            "season": found.season,
            "episode": found.episode,
            "end": found.end,
            "would_get_range": found.would_get_end,
            "matches_catalogue": agrees,
        })
    return Survey(
        name="Filename parse",
        about="What the server will make of each episode path, before a rename.",
        columns=columns,
        rows=rows,
        summary={
            "episode paths": len(rows),
            "would be read as a range": ranges,
            "claimed by no expression": unclaimed,
            "disagreeing with the catalogue": disagreements,
        },
        scope={"types": "Episode", "items seen": len(items),
               "predictor checked against": PARSED_AGAINST},
        caveats=[
            f"The predictor reproduces the expressions of {PARSED_AGAINST}. A "
            "different release may read a name differently, and the prediction is "
            "only as good as that pinning.",
            "A row that disagrees with the catalogue is not necessarily wrong: an "
            "item numbered by hand disagrees with its own filename on purpose, and "
            "that is exactly what a range-producing name forces somebody to do.",
            "Items with no path are skipped rather than counted as unparseable.",
        ],
    )
