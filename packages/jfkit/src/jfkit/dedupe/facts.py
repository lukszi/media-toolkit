"""What each copy actually holds, read from the file rather than the catalogue.

The catalogue records what the server made of a file the last time it looked,
and it is the file that is about to be parked. So every member of a group is
probed: one read of its headers (streams and format) through the probe
program, plus a listing of its folder for external subtitle files that
belong to it. Nothing here decodes; the payload check of the copy that is
kept is a separate, much heavier read (:mod:`mkvkit.integrity`).

The answer is a :class:`Copy`: its audio and subtitle tracks with the
language canonicalised, the channel count, whether a track is lossless or a
commentary (both by the owner's policy, see :class:`mkvkit.config.DedupePolicy`),
the picture size, the bitrate, and whether the name says source or re-encode.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mkvkit.config import Config, DedupePolicy
from mkvkit.langcodes import canonical
from mkvkit.probe import ffprobe_json
from mkvkit.run import Runner
from mkvkit.sidecars import SidecarKind, sidecars_of

from .groups import Member

__all__ = [
    "UNDETERMINED",
    "Copy",
    "Prober",
    "Track",
    "copy_from_probe",
    "external_subtitles",
    "origin_of",
    "probe_copy",
    "resolution_class",
]

#: The language of a track that states none.
UNDETERMINED = "und"

#: Words in an external subtitle's name that are not a language.
_NOT_LANGUAGES = frozenset({"sdh", "cc", "hi", "forced", "default", "full", "sub", "subs"})


@dataclass(frozen=True)
class Track:
    """One audio or subtitle track, as far as the rules care."""

    kind: str
    language: str
    codec: str = ""
    profile: str | None = None
    channels: int = 0
    title: str | None = None
    forced: bool = False
    commentary: bool = False
    lossless: bool = False
    #: a subtitle file beside the video rather than a track inside it
    external: bool = False

    def describe(self) -> str:
        if self.kind == "audio":
            text = f"{self.language} {self.codec or '?'} {self.channels} ch"
            if self.lossless:
                text += " lossless"
        else:
            text = f"{self.language} {self.codec or '?'}"
            if self.forced:
                text += " forced"
            if self.external:
                text += " (external)"
        if self.commentary:
            text += " commentary"
        return text


@dataclass(frozen=True)
class Copy:
    """One member of a group, with what its file holds."""

    member: Member
    size: int = 0
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    bitrate: int | None = None
    audio: tuple[Track, ...] = ()
    subtitles: tuple[Track, ...] = ()
    #: ``source``, ``re-encode`` or ``unknown``, and the word that decided it
    origin: str = "unknown"
    origin_why: str = ""
    external: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> Path:
        return Path(self.member.path)

    @property
    def resolution(self) -> int:
        return resolution_class(self.width, self.height)

    @property
    def lossless(self) -> bool:
        return any(t.lossless and not t.commentary for t in self.audio)

    @property
    def channels(self) -> int:
        return max((t.channels for t in self.audio if not t.commentary), default=0)

    def summary(self) -> str:
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        parts.append(f"{self.channels} ch")
        if self.lossless:
            parts.append("lossless")
        parts.append(self.origin)
        if self.duration_s:
            parts.append(f"{self.duration_s / 60:.1f} min")
        parts.append(f"{self.size / 2**30:.2f} GiB")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.member.item_id,
            "path": self.member.path,
            "size": self.size,
            "duration_s": self.duration_s,
            "width": self.width,
            "height": self.height,
            "resolution_class": self.resolution,
            "video_codec": self.video_codec,
            "bitrate": self.bitrate,
            "origin": self.origin,
            "audio": [t.describe() for t in self.audio],
            "subtitles": [t.describe() for t in self.subtitles],
        }


def resolution_class(width: int | None, height: int | None) -> int:
    """The picture size as a class, by its width or its 16:9 equivalent height.

    A scope film cropped to 1920x800 and the same film letterboxed to
    1920x1080 are one resolution: the class is the larger of the width and
    the width a 16:9 picture of that height would have, rounded to the
    nearest common tier. A 4:3 picture 1440x1080 is the same class too.
    """
    if not width or not height:
        return 0
    wide = max(width, round(height * 16 / 9))
    for tier in (7680, 3840, 2560, 1920, 1280, 1024, 720):
        if wide >= tier * 0.85:
            return tier
    return wide


def _word(marker: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])")


def origin_of(path: str, policy: DedupePolicy) -> tuple[str, str]:
    """``re-encode``, ``source`` or ``unknown``, by words in the file and folder name.

    A re-encode word wins over a source word: a name that says both is an
    encode made from that source.
    """
    parts = path.replace("\\", "/").rsplit("/", 2)[-2:]
    text = " ".join(parts).lower()
    for marker in policy.reencode_markers:
        if _word(marker).search(text):
            return "re-encode", marker
    for marker in policy.source_markers:
        if _word(marker).search(text):
            return "source", marker
    return "unknown", ""


def _is_lossless(codec: str, profile: str | None, policy: DedupePolicy) -> bool:
    spelled = f"{codec}/{profile or ''}".lower()
    return any(
        fnmatch.fnmatchcase(spelled if "/" in pattern else codec.lower(), pattern)
        for pattern in policy.lossless_codecs
    )


def _is_commentary(title: str | None, disposition: Mapping[str, Any],
                   policy: DedupePolicy) -> bool:
    if disposition.get("comment"):
        return True
    text = (title or "").lower()
    return any(marker in text for marker in policy.commentary_markers)


def _tags(raw: Mapping[str, Any]) -> dict[str, str]:
    tags = raw.get("tags")
    if not isinstance(tags, Mapping):
        return {}
    return {str(k).lower(): str(v) for k, v in tags.items()}


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _bitrate(stream: Mapping[str, Any], tags: Mapping[str, str]) -> int | None:
    found = _int(stream.get("bit_rate"))
    if found:
        return found
    for key, value in tags.items():
        if key == "bps" or key.startswith("bps-"):
            found = _int(value)
            if found:
                return found
    return None


def external_subtitles(path: str | Path) -> tuple[Track, ...]:
    """The subtitle files beside a video that belong to it, by their name."""
    found: list[Track] = []
    for sidecar in sidecars_of(path).of_kind(SidecarKind.SUBTITLE):
        words = [w for w in sidecar.tail.lower().split(".") if w]
        codec = words[-1] if words else ""
        language = UNDETERMINED
        for word in words[:-1]:
            if word in _NOT_LANGUAGES or not word.isalpha() or len(word) not in (2, 3):
                continue
            language = canonical(word) or UNDETERMINED
            break
        found.append(Track(
            kind="subtitle", language=language, codec=codec,
            forced="forced" in words[:-1], external=True,
            title=sidecar.path.name,
        ))
    return tuple(found)


def copy_from_probe(
    member: Member,
    probed: Mapping[str, Any],
    policy: DedupePolicy,
    *,
    size: int = 0,
    external: Sequence[Track] = (),
) -> Copy:
    """A :class:`Copy` from the probe program's JSON for one file."""
    fmt = probed.get("format") or {}
    audio: list[Track] = []
    subtitles: list[Track] = []
    video: Mapping[str, Any] | None = None
    video_tags: dict[str, str] = {}
    for stream in probed.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        kind = stream.get("codec_type")
        tags = _tags(stream)
        disposition = stream.get("disposition") or {}
        if not isinstance(disposition, Mapping):
            disposition = {}
        language = canonical(tags.get("language")) or UNDETERMINED
        title = tags.get("title")
        codec = str(stream.get("codec_name") or "")
        if kind == "video":
            if disposition.get("attached_pic") or video is not None:
                continue
            video, video_tags = stream, tags
        elif kind == "audio":
            profile = stream.get("profile")
            audio.append(Track(
                kind="audio", language=language, codec=codec,
                profile=str(profile) if profile else None,
                channels=_int(stream.get("channels")) or 0, title=title,
                commentary=_is_commentary(title, disposition, policy),
                lossless=_is_lossless(codec, str(profile) if profile else None, policy),
            ))
        elif kind == "subtitle":
            subtitles.append(Track(
                kind="subtitle", language=language, codec=codec, title=title,
                forced=bool(disposition.get("forced")),
                commentary=_is_commentary(title, disposition, policy),
            ))
    origin, why = origin_of(member.path, policy)
    bitrate = _bitrate(video, video_tags) if video is not None else None
    return Copy(
        member=member,
        size=size or (_int(fmt.get("size")) or 0),
        duration_s=_float(fmt.get("duration")),
        width=_int(video.get("width")) if video is not None else None,
        height=_int(video.get("height")) if video is not None else None,
        video_codec=str(video.get("codec_name")) if video is not None else None,
        bitrate=bitrate or _int(fmt.get("bit_rate")),
        audio=tuple(audio),
        subtitles=(*subtitles, *external),
        origin=origin, origin_why=why,
    )


#: Reads one member's file into a :class:`Copy`.
Prober = Callable[[Member], Copy]


def probe_copy(
    member: Member, policy: DedupePolicy, *, runner: Runner | None = None,
    config: Config | None = None,
) -> Copy:
    """Read one member's file: its headers and its external subtitle files.

    Raises when the file is not there or the probe program cannot read it;
    the caller turns that into a blocked group.
    """
    path = Path(member.path)
    if path.is_dir():
        raise IsADirectoryError(
            f"{path.name} is a folder (a disc structure), not a file; it is not probed, "
            "and its group is left for a person"
        )
    size = path.stat().st_size
    probed = ffprobe_json(path, runner=runner, config=config, chapters=False)
    return copy_from_probe(member, probed, policy, size=size,
                           external=external_subtitles(path))
