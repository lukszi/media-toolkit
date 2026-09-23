"""What languages the audio tracks claim, per track, and what dropping them would save.

One row per audio track rather than per item, because "this film has four
audio tracks" is the summary and "the third one says it is undetermined" is
the finding.

Two things are worth being careful about here.

**A language code is a claim, not a measurement.** A track labelled ``und``
is a track nobody labelled; a track labelled ``eng`` is a track somebody
labelled ``eng``, which is a different statement from "this track is in
English". The column is called ``claimed`` for that reason, and the caveats
say so. Identifying what is actually spoken is a different job, done by
listening to the audio, and it lives in the file-side package.

**The saving is an estimate and is presented as one.** A track's own size is
not in the catalogue; what is there is the bitrate and the runtime, and their
product is what an estimate can be built from. Where a track has no bitrate
the row says so rather than contributing a zero, and the summary counts how
many rows that was -- an estimate whose unknowns are invisible is worse than
no estimate.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..dto import TICKS_PER_SECOND
from ..report import Column, Survey

__all__ = ["UNDETERMINED", "audio_languages", "estimated_bytes"]

#: The codes that mean "nobody said". They are not a language and counting
#: them as one is how a library looks better labelled than it is.
UNDETERMINED = frozenset({"", "und", "unk", "zxx", "mis", "mul"})


def estimated_bytes(stream: Mapping[str, Any], runtime_ticks: Any) -> int | None:
    """Roughly what this track costs, from its bitrate and the runtime.

    ``None`` when either number is missing, rather than nought. A missing
    value that reads as zero is the quiet way an estimate becomes wrong in the
    direction that makes the work look unnecessary.
    """
    bitrate = stream.get("BitRate")
    if not bitrate or not runtime_ticks:
        return None
    seconds = float(runtime_ticks) / TICKS_PER_SECOND
    return int(float(bitrate) * seconds / 8.0)


def audio_languages(items: Sequence[Mapping[str, Any]]) -> Survey:
    """One row per audio track: what it claims to be, and what it costs."""
    columns = [
        Column("item", "Item"),
        Column("type", "Type"),
        Column("index", "Stream", kind="number"),
        Column("claimed", "Claimed language"),
        Column("codec", "Codec"),
        Column("channels", "Channels", kind="number"),
        Column("default", "Default", kind="bool"),
        Column("determined", "Labelled", kind="bool"),
        Column("estimated_mib", "Estimated size (MiB)", kind="number", places=1),
    ]
    rows: list[dict[str, Any]] = []
    per_language: dict[str, int] = {}
    per_language_bytes: dict[str, int] = {}
    unknown_size = 0
    items_with_audio = 0
    no_default = 0
    many_defaults = 0

    for item in items:
        streams = [
            stream for stream in (item.get("MediaStreams") or [])
            if stream.get("Type") == "Audio"
        ]
        if not streams:
            continue
        items_with_audio += 1
        defaults = sum(1 for stream in streams if stream.get("IsDefault"))
        no_default += defaults == 0
        many_defaults += defaults > 1
        for stream in streams:
            claimed = (stream.get("Language") or "").lower()
            size = estimated_bytes(stream, item.get("RunTimeTicks"))
            if size is None:
                unknown_size += 1
            else:
                per_language_bytes[claimed] = per_language_bytes.get(claimed, 0) + size
            per_language[claimed] = per_language.get(claimed, 0) + 1
            rows.append({
                "item": item.get("Name"),
                "type": item.get("Type"),
                "index": stream.get("Index"),
                "claimed": claimed or "(none)",
                "codec": (stream.get("Codec") or "").lower(),
                "channels": stream.get("Channels"),
                "default": bool(stream.get("IsDefault")),
                "determined": claimed not in UNDETERMINED,
                "estimated_mib": None if size is None else size / 2**20,
            })

    summary: dict[str, Any] = {
        "items with audio": items_with_audio,
        "audio tracks": len(rows),
        "tracks with no language claimed": sum(
            count for code, count in per_language.items() if code in UNDETERMINED
        ),
        "items with no default track": no_default,
        "items with more than one default track": many_defaults,
        "tracks whose size could not be estimated": unknown_size,
    }
    for code in sorted(per_language, key=lambda c: (-per_language[c], c)):
        estimate = per_language_bytes.get(code, 0) / 2**30
        summary[f"tracks claiming {code or '(none)'}"] = (
            f"{per_language[code]} (~{estimate:.1f} GiB)"
        )
    return Survey(
        name="Audio languages",
        about="One row per audio track: the language it claims, and what it costs.",
        columns=columns,
        rows=rows,
        summary=summary,
        scope={"items seen": len(items), "items with audio": items_with_audio},
        caveats=[
            "A language code is a claim made by whoever tagged the track, not a "
            "measurement of what is spoken. Treat it as a label to be checked.",
            "Sizes are estimated from each track's bitrate and the item's runtime, "
            "because a per-track size is not in the catalogue. Tracks with no "
            "bitrate contribute nothing and are counted separately.",
            "A track labelled with one of the undetermined codes is counted as "
            "unlabelled rather than as a language of its own.",
        ],
    )
