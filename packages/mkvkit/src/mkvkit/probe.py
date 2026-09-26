"""mkvkit.probe -- read a file once, with both programs, into one typed answer.

Two programs can describe a media file and they do not describe the same
thing. The muxer's identification output knows the container: track
identifiers, the unique identifier each track carries, the flags, the
attachments, how many chapter editions there are. The probe from the decoding
library knows what a *player* will make of it: the stream order, the
dispositions, and -- crucially -- the metadata a demuxer resolves, which is
not always what the container's own header says.

Everything in this package that reads a file starts here, so the awkward parts
are handled in one place:

**The extension is not the container.** A file named for Matroska may be
another container entirely. The header editor writes Matroska only and does
nothing at all to such a file, exit code zero and no message, so anything that
writes has to ask first. :func:`container_mismatch` is that question.

**The two programs spell a language code differently.** For a language with
two ISO 639-2 spellings the muxer reports the bibliographic one and the probe
reports the terminological one -- the same track comes back as ``fre`` from
one and ``fra`` from the other. Comparing them raw invents a disagreement, so
every code that leaves this module is canonicalised, with the raw spelling
kept beside it for a report.

**A tag can overrule the track header, and both programs say so if asked.**
The identification output carries the tag's value in a property of its own
beside the header's; the probe returns the key ``language`` when the value
came from the header and ``LANGUAGE`` when it came from a tag, so even the
case of a key is evidence. Both are kept -- :attr:`Track.tag_language` and
:attr:`Stream.language_from_tag` -- and
:attr:`MediaProbe.language_disagreements` collects them, because a caller
about to set a header needs to be told beforehand that doing so will change
nothing anybody sees.

**A seek index is not in either answer.** Whether the container carries one is
read from the element structure itself (:mod:`mkvkit.ebml`), because a file
without one turns every seek into a read from byte zero.

Nothing here writes. Nothing here decodes a packet: the whole module is
identification and metadata, so it costs one open and a few seeks rather than
a pass over the file.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ebml
from .config import Config
from .langcodes import canonical
from .run import Runner, default_runner

__all__ = [
    "Attachment",
    "ChapterMark",
    "Container",
    "LanguageDisagreement",
    "MediaProbe",
    "ProbeError",
    "Stream",
    "Track",
    "container_mismatch",
    "duration_disagreements",
    "ffprobe_json",
    "identify_json",
    "probe",
    "probe_from_json",
]

log = logging.getLogger(__name__)

#: Extensions that promise a Matroska container.
MATROSKA_SUFFIXES = frozenset({".mkv", ".mka", ".mks", ".mk3d", ".webm"})

#: Exit codes the muxer uses for "identified it, with warnings".
_IDENTIFY_OK = (0, 1)


class ProbeError(RuntimeError):
    """A program answered, and the answer could not be used."""


# ------------------------------------------------------------------ data shapes
@dataclass(frozen=True)
class Track:
    """One track, as the container itself describes it."""

    id: int
    type: str
    codec: str | None = None
    codec_id: str | None = None
    language: str | None = None
    language_raw: str | None = None
    language_ietf: str | None = None
    #: What a tag element claims for this track, where one does.
    tag_language: str | None = None
    tag_language_raw: str | None = None
    name: str | None = None
    default: bool = False
    forced: bool = False
    enabled: bool = True
    channels: int | None = None
    sample_rate: int | None = None
    pixel_dimensions: str | None = None
    display_dimensions: str | None = None
    uid: int | None = None
    number: int | None = None

    @property
    def is_audio(self) -> bool:
        return self.type == "audio"

    @property
    def effective_language(self) -> str | None:
        """What a consumer will read: the tag where there is one, else the header."""
        return self.tag_language if self.tag_language is not None else self.language


@dataclass(frozen=True)
class Attachment:
    name: str | None = None
    size: int | None = None
    content_type: str | None = None
    uid: int | None = None


@dataclass(frozen=True)
class Container:
    """What the file actually is, as opposed to what it is called."""

    type: str | None = None
    duration_s: float | None = None
    title: str | None = None
    recognized: bool = True
    supported: bool = True

    @property
    def is_matroska(self) -> bool:
        return "matroska" in (self.type or "").lower()


@dataclass(frozen=True)
class Stream:
    """One stream as a decoding library resolves it, metadata included."""

    index: int
    type: str
    codec: str | None = None
    channels: int | None = None
    start_time_s: float | None = None
    duration_s: float | None = None
    language: str | None = None
    language_raw: str | None = None
    #: True when the language came from a tag element rather than the header.
    language_from_tag: bool = False
    title: str | None = None
    disposition: Mapping[str, int] = field(default_factory=dict)
    #: the duration the track's own statistics tag states (Matroska writes
    #: one per track), where the stream itself states none
    tagged_duration_s: float | None = None

    @property
    def stated_duration_s(self) -> float | None:
        """The track's duration as its headers state it, from either place."""
        return self.duration_s if self.duration_s is not None else self.tagged_duration_s


@dataclass(frozen=True)
class LanguageDisagreement:
    """A track whose header and whose resolved language are not the same.

    Every one of these is a file where an edit to the header alone leaves
    every consumer still reading the old value.
    """

    track_id: int
    header: str | None
    effective: str | None
    source: str

    def __str__(self) -> str:
        return (
            f"track {self.track_id}: the header says {self.header or 'nothing'}, "
            f"{self.source} says {self.effective or 'nothing'}"
        )


@dataclass(frozen=True)
class ChapterMark:
    """One chapter as a player sees it. The document itself is in :mod:`mkvkit.chapters`."""

    start_s: float
    end_s: float | None = None
    title: str | None = None


@dataclass(frozen=True)
class MediaProbe:
    """Everything one reading of a file can say about it without decoding."""

    path: Path
    container: Container = field(default_factory=Container)
    tracks: tuple[Track, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    chapter_count: int = 0
    edition_count: int = 0
    tag_count: int = 0
    streams: tuple[Stream, ...] = ()
    chapters: tuple[ChapterMark, ...] = ()
    format_duration_s: float | None = None
    size: int | None = None
    #: Top-level element names, where they could be read. ``None`` means "not asked".
    elements: frozenset[str] | None = None

    # ------------------------------------------------------------- selectors
    @property
    def audio(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if t.type == "audio")

    @property
    def video(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if t.type == "video")

    @property
    def subtitles(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if t.type == "subtitles")

    def track(self, track_id: int) -> Track | None:
        for candidate in self.tracks:
            if candidate.id == track_id:
                return candidate
        return None

    def track_by_uid(self, uid: int) -> Track | None:
        for candidate in self.tracks:
            if candidate.uid == uid:
                return candidate
        return None

    def stream(self, index: int) -> Stream | None:
        for candidate in self.streams:
            if candidate.index == index:
                return candidate
        return None

    def audio_ordinal(self, track_id: int) -> int | None:
        """The position of a track among the audio tracks, counting from one.

        The header editor selects a track by that ordinal, not by the
        identifier the muxer prints, and confusing the two edits the wrong
        track on any file whose audio does not start at the top.
        """
        for position, candidate in enumerate(self.audio, start=1):
            if candidate.id == track_id:
                return position
        return None

    # -------------------------------------------------------------- verdicts
    @property
    def has_cues(self) -> bool | None:
        """Whether the container carries a seek index. ``None`` if not read."""
        if self.elements is None:
            return None
        return "Cues" in self.elements

    @property
    def language_disagreements(self) -> tuple[LanguageDisagreement, ...]:
        """Every track whose header is not what a consumer will read.

        Both programs are asked. The identification output names the tag's
        value directly; the probe's answer is a cross-check that also covers
        a container this package cannot edit anyway.
        """
        out: list[LanguageDisagreement] = []
        for track in self.tracks:
            if track.tag_language is not None and track.tag_language != track.language:
                out.append(
                    LanguageDisagreement(
                        track.id, track.language, track.tag_language, "a tag element"
                    )
                )
                continue
            found = self.stream(track.id)
            if found is None or (track.language is None and found.language is None):
                continue
            if track.language != found.language:
                out.append(
                    LanguageDisagreement(
                        track.id,
                        track.language,
                        found.language,
                        "a tag element" if found.language_from_tag else "the demuxer",
                    )
                )
        return tuple(out)


# ----------------------------------------------------------------- collection
def identify_json(
    path: Path | str, *, runner: Runner | None = None, config: Config | None = None
) -> dict[str, Any]:
    """The muxer's identification output, parsed."""
    run = runner if runner is not None else default_runner(config)
    result = run("mkvmerge", ["-J", str(path)], ok=_IDENTIFY_OK)
    try:
        parsed: dict[str, Any] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{path}: identification output is not usable: {exc}") from exc
    return parsed


def ffprobe_json(
    path: Path | str,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
    chapters: bool = True,
) -> dict[str, Any]:
    """Streams, format and (optionally) chapters, parsed."""
    run = runner if runner is not None else default_runner(config)
    args = ["-v", "error", "-of", "json", "-show_streams", "-show_format"]
    if chapters:
        args.append("-show_chapters")
    result = run("ffprobe", [*args, str(path)])
    try:
        parsed: dict[str, Any] = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{path}: probe output is not usable: {exc}") from exc
    return parsed


def probe(
    path: Path | str,
    *,
    runner: Runner | None = None,
    config: Config | None = None,
    elements: bool = True,
) -> MediaProbe:
    """Read one file with both programs and return the combined answer."""
    target = Path(path)
    run = runner if runner is not None else default_runner(config)
    identified = identify_json(target, runner=run)
    probed = ffprobe_json(target, runner=run)
    top: frozenset[str] | None = None
    if elements:
        top = read_elements(target)
    return probe_from_json(target, identified, probed, elements=top)


def read_elements(path: Path | str) -> frozenset[str] | None:
    """Top-level element names, or ``None`` where they cannot be read.

    A container that is not Matroska has no such structure, and a file that
    cannot be opened is a problem for the caller that wanted the bytes, not
    for a metadata read.
    """
    try:
        return ebml.scan(path).names
    except OSError as exc:
        log.debug("%s: element scan failed: %s", path, exc)
        return None


# --------------------------------------------------------------------- parsing
def probe_from_json(
    path: Path | str,
    identified: Mapping[str, Any],
    probed: Mapping[str, Any] | None = None,
    *,
    elements: Iterable[str] | None = None,
    size: int | None = None,
) -> MediaProbe:
    """Build the typed answer from the two programs' output.

    Separated from the running so the parsing -- where the interesting
    mistakes live -- is testable with nothing installed.
    """
    target = Path(path)
    container = _container(identified)
    tracks = tuple(_track(raw) for raw in _sequence(identified, "tracks"))
    attachments = tuple(_attachment(raw) for raw in _sequence(identified, "attachments"))
    editions = _sequence(identified, "chapters")
    chapter_count = sum(int(e.get("num_entries") or 0) for e in editions)
    # both tag counts arrive as lists of one entry, never as a single object
    tag_count = sum(
        int(t.get("num_entries") or 0)
        for key in ("global_tags", "track_tags")
        for t in _sequence(identified, key)
    )
    streams: tuple[Stream, ...] = ()
    marks: tuple[ChapterMark, ...] = ()
    format_duration: float | None = None
    if probed is not None:
        streams = tuple(_stream(raw) for raw in _sequence(probed, "streams"))
        marks = tuple(_mark(raw) for raw in _sequence(probed, "chapters"))
        fmt = probed.get("format") or {}
        format_duration = _float(fmt.get("duration"))
        if size is None:
            size = _int(fmt.get("size"))
    if size is None:
        try:
            size = target.stat().st_size
        except OSError:
            size = None
    return MediaProbe(
        path=target,
        container=container,
        tracks=tracks,
        attachments=attachments,
        chapter_count=chapter_count,
        edition_count=len(editions),
        tag_count=tag_count,
        streams=streams,
        chapters=marks,
        format_duration_s=format_duration,
        size=size,
        elements=None if elements is None else frozenset(elements),
    )


def container_mismatch(found: MediaProbe) -> str | None:
    """One sentence when the name promises Matroska and the file is not, else ``None``.

    This is the guard every writing path in this package calls first. The
    header editor does nothing to such a file and says nothing about it, so
    without the guard the pass reports success for ever.
    """
    if found.path.suffix.lower() not in MATROSKA_SUFFIXES:
        return None
    if found.container.is_matroska:
        return None
    actual = found.container.type or "an unrecognised container"
    return (
        f"{found.path.name} is named for Matroska but the container is {actual}; "
        "a header edit would do nothing at all here, so it has to be rebuilt instead"
    )


# ----------------------------------------------------------------------- detail
def _sequence(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _container(identified: Mapping[str, Any]) -> Container:
    raw = identified.get("container")
    if not isinstance(raw, Mapping):
        return Container()
    properties = raw.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    duration_ns = _int(properties.get("duration"))
    return Container(
        type=_text(raw.get("type")),
        duration_s=None if duration_ns is None else duration_ns / 1e9,
        title=_text(properties.get("title")),
        recognized=bool(raw.get("recognized", True)),
        supported=bool(raw.get("supported", True)),
    )


def _track(raw: Mapping[str, Any]) -> Track:
    properties = raw.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    language_raw = _text(properties.get("language"))
    tag_language_raw = _text(properties.get("tag_language"))
    return Track(
        id=int(raw.get("id") or 0),
        type=str(raw.get("type") or ""),
        codec=_text(raw.get("codec")),
        codec_id=_text(properties.get("codec_id")),
        language=canonical(language_raw),
        language_raw=language_raw,
        language_ietf=_text(properties.get("language_ietf")),
        tag_language=canonical(tag_language_raw),
        tag_language_raw=tag_language_raw,
        name=_text(properties.get("track_name")),
        default=bool(properties.get("default_track", False)),
        forced=bool(properties.get("forced_track", False)),
        enabled=bool(properties.get("enabled_track", True)),
        channels=_int(properties.get("audio_channels")),
        sample_rate=_int(properties.get("audio_sampling_frequency")),
        pixel_dimensions=_text(properties.get("pixel_dimensions")),
        display_dimensions=_text(properties.get("display_dimensions")),
        uid=_int(properties.get("uid")),
        number=_int(properties.get("number")),
    )


def _attachment(raw: Mapping[str, Any]) -> Attachment:
    properties = raw.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    return Attachment(
        name=_text(raw.get("file_name")),
        size=_int(raw.get("size")),
        content_type=_text(raw.get("content_type")),
        uid=_int(properties.get("uid")),
    )


def _stream(raw: Mapping[str, Any]) -> Stream:
    tags = raw.get("tags")
    tags = tags if isinstance(tags, Mapping) else {}
    language_raw: str | None = None
    from_tag = False
    for key, value in tags.items():
        if str(key).lower() != "language":
            continue
        language_raw = _text(value)
        # The case of the key is the evidence: upper case means the value was
        # resolved from a tag element, which overrides the track header.
        from_tag = str(key).isupper()
        break
    title: str | None = None
    for key, value in tags.items():
        if str(key).lower() == "title":
            title = _text(value)
            break
    tagged: float | None = None
    for key, value in tags.items():
        if str(key).lower().split("-")[0] == "duration":
            tagged = _clock(value)
            break
    disposition = raw.get("disposition")
    return Stream(
        index=int(raw.get("index") or 0),
        type=str(raw.get("codec_type") or ""),
        codec=_text(raw.get("codec_name")),
        channels=_int(raw.get("channels")),
        start_time_s=_float(raw.get("start_time")),
        duration_s=_float(raw.get("duration")),
        language=canonical(language_raw),
        language_raw=language_raw,
        language_from_tag=from_tag,
        title=title,
        disposition=(
            {str(k): int(v) for k, v in disposition.items()}
            if isinstance(disposition, Mapping)
            else {}
        ),
        tagged_duration_s=tagged,
    )


def _clock(value: Any) -> float | None:
    """``01:02:03.500000000`` as seconds, or None."""
    text = _text(value)
    if not text:
        return None
    parts = text.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


#: A track may end this much before or after the container without comment:
#: an audio track routinely stops a frame or two short, and a few seconds is
#: an edit, not a hole.
DURATION_TOLERANCE_S = 5.0


def duration_disagreements(
    found: MediaProbe, *, tolerance_s: float = DURATION_TOLERANCE_S
) -> list[str]:
    """Audio and video tracks whose stated duration is not the container's.

    Stated, not measured: these come from headers, which are exactly what a
    broken file keeps. A disagreement is a reason to run the payload check
    (:mod:`mkvkit.integrity`); agreement is not evidence the payload is there.
    """
    container = found.format_duration_s or found.container.duration_s
    if not container:
        return []
    out: list[str] = []
    for stream in found.streams:
        if stream.type not in ("audio", "video") or stream.disposition.get("attached_pic"):
            continue
        stated = stream.stated_duration_s
        if stated is None or abs(stated - container) <= tolerance_s:
            continue
        out.append(
            f"stream {stream.index} ({stream.type}) states {stated:.1f} s, "
            f"the container {container:.1f} s"
        )
    return out


def _mark(raw: Mapping[str, Any]) -> ChapterMark:
    tags = raw.get("tags")
    tags = tags if isinstance(tags, Mapping) else {}
    title = None
    for key, value in tags.items():
        if str(key).lower() == "title":
            title = _text(value)
            break
    return ChapterMark(
        start_s=_float(raw.get("start_time")) or 0.0,
        end_s=_float(raw.get("end_time")),
        title=title,
    )


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number  # a probe can answer "not a number"


def describe(found: MediaProbe) -> Sequence[str]:
    """A few lines a person can read, for the command line and for a log."""
    lines = [
        f"{found.path.name}: {found.container.type or 'unknown container'}"
        f"{'' if found.container.duration_s is None else f', {found.container.duration_s:.3f} s'}"
        f"{'' if found.size is None else f', {found.size} bytes'}"
    ]
    for track in found.tracks:
        flags = [name for name, on in
                 (("default", track.default), ("forced", track.forced)) if on]
        if not track.enabled:
            flags.append("disabled")
        lines.append(
            f"  track {track.id:>2} {track.type:<9} {track.codec or '?':<20} "
            f"{track.language or 'und':<4}"
            f"{' ' + track.language_ietf if track.language_ietf else ''}"
            f"{' [' + ', '.join(flags) + ']' if flags else ''}"
            f"{' ' + repr(track.name) if track.name else ''}"
            f"{f'  uid {track.uid}' if track.uid is not None else ''}"
        )
    if found.chapter_count:
        lines.append(
            f"  {found.chapter_count} chapter(s) in {found.edition_count} edition(s)"
        )
    if found.attachments:
        lines.append(f"  {len(found.attachments)} attachment(s)")
    if found.elements is not None:
        lines.append(f"  seek index: {'yes' if found.has_cues else 'NO'}")
    for disagreement in found.language_disagreements:
        lines.append(f"  {disagreement}")
    disagreements = duration_disagreements(found)
    if disagreements:
        lines.append(
            "  WARNING: the tracks' durations disagree with the container's; "
            "run `mkvkit integrity` before relying on this file"
        )
        for stream in found.streams:
            if stream.type in ("audio", "video") and stream.stated_duration_s is not None:
                lines.append(
                    f"    stream {stream.index} {stream.type:<5} "
                    f"{stream.stated_duration_s:.1f} s"
                )
        lines += [f"    {d}" for d in disagreements]
    return lines
