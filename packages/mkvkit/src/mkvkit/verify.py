"""mkvkit.verify -- prove the new file is the old one plus the change you asked for.

"The command exited zero" is not verification. Neither is "the file is about
the right size". This module collects evidence from both files and compares
it against a *declared* expectation, so the question it answers is not "are
these the same?" but "is the difference exactly the one that was intended?".

**The primary evidence is a hash per stream.** Ask the decoder for one hash
per stream with a stream copy -- no decoding, no re-encoding -- on both sides,
drop the deliberately removed streams from the original's list, and compare
the ordered lists. A mismatch is located by position: this hash was that
stream. Check the exit code on both sides too, because a non-zero exit with a
plausible-looking list of hashes is a failure, not a pass. This is a full read
of both files and it is the expensive step of any verification; on separate
physical disks the two sides read in parallel for free.

**Know what cannot have changed, and skip measuring it.** A header edit cannot
touch a packet payload. So after one, :func:`reheader` refreshes the header
half of the evidence and keeps the hashes that were already collected, which
turns an hours-long re-read into a second. That is not a shortcut; it is the
difference between a verification that runs and one that gets skipped.

**Some differences are notes, not failures, and the reasons are the point.**
A modern language subtag appearing while the legacy element is unchanged; a
regenerated track or chapter identifier that nothing outside the file
references; a container duration that *shrank* while every kept stream hash
matched, which is what dropping the longest track looks like. Each is recorded
with its reason rather than silently ignored, and each keeps its teeth: two
files carrying *different* modern subtags still fails, and a duration that
grew still fails.

**A missing seek index is a failure.** It costs nothing to write and it turns
every later seek into a read from the start of the file.

**Audio timing is compared to forty milliseconds.** That is the bar this
discipline uses everywhere: a difference below it is not audible against
picture, and a difference above it is a fault to be explained rather than
averaged away.

When a video stream's hash differs, that is not yet a re-encode:
:func:`frames_identical` compares the coded pictures. Identical frame hashes
with a different stream hash means the container re-stated its parameter sets,
which changes the bytes and not one picture.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .chapters import xml as chapters_xml
from .config import Config
from .probe import MediaProbe, probe
from .run import Runner, default_runner
from .tags import TagSet, read_tags

__all__ = [
    "ACCOUNTING_TAGS",
    "AUDIO_TIMING_TOLERANCE_S",
    "CHAPTER_TOLERANCE_S",
    "DURATION_TOLERANCE_S",
    "Comparison",
    "Evidence",
    "ExpectedDelta",
    "HeaderOnly",
    "StreamHash",
    "TracksAppended",
    "TracksDropped",
    "collect",
    "compare",
    "describe",
    "frame_hashes",
    "frames_identical",
    "reheader",
    "stream_hashes",
]

log = logging.getLogger(__name__)

#: The bar for audio timing, everywhere in this toolkit.
AUDIO_TIMING_TOLERANCE_S: Final = 0.040
#: A chapter mark that moved by less than this did not move.
CHAPTER_TOLERANCE_S: Final = 0.001
#: Container durations are compared this loosely; see the note about shrinking.
DURATION_TOLERANCE_S: Final = 1.0

#: Tags a muxer writes about its own output rather than about the work: how
#: many bytes and frames a track has, how long it runs, which build wrote it.
#: A rebuild recomputes every one of them, so comparing them across a rebuild
#: measures the muxer and not the content.
ACCOUNTING_TAGS: Final[frozenset[str]] = frozenset(
    {
        "BPS",
        "DURATION",
        "NUMBER_OF_BYTES",
        "NUMBER_OF_FRAMES",
        "ENCODER",
        "SOURCE_ID",
        "_STATISTICS_TAGS",
        "_STATISTICS_WRITING_APP",
        "_STATISTICS_WRITING_DATE_UTC",
    }
)


@dataclass(frozen=True)
class StreamHash:
    """One stream's payload, as the decoder hashes it with a stream copy."""

    index: int
    type: str
    algorithm: str
    value: str


@dataclass(frozen=True)
class Evidence:
    """Everything one side of a comparison has to offer."""

    path: Path
    probe: MediaProbe
    hashes: tuple[StreamHash, ...] = ()
    chapters: chapters_xml.ChapterSet = field(
        default_factory=chapters_xml.ChapterSet
    )
    tags: TagSet = field(default_factory=TagSet)
    hashed: bool = True

    @property
    def size(self) -> int | None:
        return self.probe.size

    def hash_at(self, index: int) -> StreamHash | None:
        for entry in self.hashes:
            if entry.index == index:
                return entry
        return None


# --------------------------------------------------------------- the expectation
@dataclass(frozen=True, kw_only=True)
class ExpectedDelta:
    """What may be different. The base case: nothing at all.

    A comparison without one of these is not a verification, because it has
    no way to tell an intended change from an accident.
    """

    label: str = "no change"

    def kept(self, original: Evidence) -> tuple[int, ...]:
        """Stream indexes of the original that must survive, in order."""
        return tuple(stream.index for stream in original.probe.streams)

    @property
    def appended(self) -> int:
        """How many new streams may appear after the kept ones."""
        return 0

    @property
    def may_change(self) -> frozenset[str]:
        """Track fields a caller has declared may differ."""
        return frozenset()

    @property
    def payload_may_change(self) -> bool:
        return False

    @property
    def one_default_audio(self) -> bool:
        """Whether the new file must carry exactly one default audio track."""
        return False

    @property
    def chapters(self) -> chapters_xml.ChapterSet | None:
        """The marks the new file must carry, when they were replaced on purpose.

        None means the marks were not touched and are compared with the
        original's.
        """
        return None


@dataclass(frozen=True, kw_only=True)
class TracksDropped(ExpectedDelta):
    """Streams were removed on purpose, and nothing else was touched."""

    dropped: frozenset[int] = frozenset()
    default_moved: bool = False
    #: A chapter document written in by the same rebuild, if there was one.
    replaced_chapters: chapters_xml.ChapterSet | None = None
    label: str = "tracks dropped"

    def kept(self, original: Evidence) -> tuple[int, ...]:
        return tuple(
            stream.index
            for stream in original.probe.streams
            if stream.index not in self.dropped
        )

    @property
    def may_change(self) -> frozenset[str]:
        return frozenset({"default"}) if self.default_moved else frozenset()

    @property
    def one_default_audio(self) -> bool:
        # Moving the default is only half an edit if the old default keeps its
        # flag too: a player then picks whichever it meets first. So a moved
        # default is allowed to change the flag, and required to leave one.
        return self.default_moved

    @property
    def chapters(self) -> chapters_xml.ChapterSet | None:
        return self.replaced_chapters


@dataclass(frozen=True, kw_only=True)
class TracksAppended(ExpectedDelta):
    """Streams were added at the end; every original stream is still there."""

    count: int = 0
    label: str = "tracks appended"

    @property
    def appended(self) -> int:
        return self.count


@dataclass(frozen=True, kw_only=True)
class HeaderOnly(ExpectedDelta):
    """Only the named header fields may differ. No payload can have moved."""

    fields: frozenset[str] = frozenset({"language", "name", "default", "forced"})
    label: str = "header only"

    @property
    def may_change(self) -> frozenset[str]:
        return self.fields


# ------------------------------------------------------------------- collection
def stream_hashes(
    path: Path | str,
    *,
    algorithm: str = "md5",
    runner: Runner | None = None,
    config: Config | None = None,
) -> tuple[StreamHash, ...]:
    """One hash per stream, from a stream copy. A full read of the file."""
    run = runner if runner is not None else default_runner(config)
    result = run(
        "ffmpeg",
        [
            "-nostdin", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
            "-f", "streamhash", "-hash", algorithm, "-",
        ],
    )
    out: list[StreamHash] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        value = parts[-1]
        algorithm_name, _, digest = value.partition("=")
        out.append(
            StreamHash(
                index=index,
                type=parts[1].strip(),
                algorithm=algorithm_name.strip() if digest else algorithm,
                value=(digest or value).strip(),
            )
        )
    return tuple(out)


def frame_hashes(
    path: Path | str,
    *,
    stream: str = "v:0",
    runner: Runner | None = None,
    config: Config | None = None,
) -> tuple[str, ...]:
    """One hash per coded frame of one stream, for telling a re-encode apart."""
    run = runner if runner is not None else default_runner(config)
    result = run(
        "ffmpeg",
        [
            "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{stream}",
            "-c", "copy", "-f", "framemd5", "-",
        ],
    )
    return tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("#")
    )


def frames_identical(
    original: Path | str,
    built: Path | str,
    *,
    stream: str = "v:0",
    runner: Runner | None = None,
    config: Config | None = None,
) -> bool:
    """Whether the coded pictures are the same, whatever the container did.

    A different stream hash with identical frame hashes is a container that
    re-stated its parameter sets: different bytes, not one different picture.
    """
    run = runner if runner is not None else default_runner(config)
    left = frame_hashes(original, stream=stream, runner=run)
    right = frame_hashes(built, stream=stream, runner=run)
    return bool(left) and left == right


def collect(
    path: Path | str,
    *,
    hashes: bool = True,
    runner: Runner | None = None,
    config: Config | None = None,
) -> Evidence:
    """Every piece of evidence for one file. The hashes are the expensive half."""
    target = Path(path)
    run = runner if runner is not None else default_runner(config)
    found = probe(target, runner=run)
    chapters = (
        chapters_xml.read_chapters(target, runner=run)
        if found.chapter_count
        else chapters_xml.ChapterSet()
    )
    tags = read_tags(target, runner=run) if found.tag_count else TagSet()
    collected = stream_hashes(target, runner=run) if hashes else ()
    return Evidence(
        path=target,
        probe=found,
        hashes=collected,
        chapters=chapters,
        tags=tags,
        hashed=hashes,
    )


def reheader(
    evidence: Evidence, *, runner: Runner | None = None, config: Config | None = None
) -> Evidence:
    """Re-read the header half and keep the hashes already collected.

    Only correct after an edit that provably cannot have touched a payload --
    a header edit. Used anywhere else it is a lie, which is why it takes the
    old evidence rather than a path: the hashes it keeps are the ones somebody
    already paid for.
    """
    run = runner if runner is not None else default_runner(config)
    found = probe(evidence.path, runner=run)
    chapters = (
        chapters_xml.read_chapters(evidence.path, runner=run)
        if found.chapter_count
        else chapters_xml.ChapterSet()
    )
    tags = read_tags(evidence.path, runner=run) if found.tag_count else TagSet()
    return replace(evidence, probe=found, chapters=chapters, tags=tags)


# ------------------------------------------------------------------- comparison
@dataclass(frozen=True)
class Comparison:
    """The verdict, and the reasons -- both the ones that fail and the ones that do not."""

    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    hashed_streams: int = 0
    label: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        head = (
            f"{'PASS' if self.ok else 'FAIL'} ({self.label}): "
            f"{self.hashed_streams} stream(s) compared by hash"
        )
        return "\n".join(
            [head, *(f"  problem: {p}" for p in self.problems),
             *(f"  note: {n}" for n in self.notes)]
        )


def compare(
    original: Evidence, built: Evidence, delta: ExpectedDelta | None = None
) -> Comparison:
    """Compare two sides against a declared expectation. No I/O, all decisions."""
    expectation = delta if delta is not None else ExpectedDelta()
    problems: list[str] = []
    notes: list[str] = []

    kept = expectation.kept(original)
    hashed = _compare_hashes(original, built, kept, expectation, problems, notes)
    _compare_tracks(original, built, kept, expectation, problems, notes)
    if expectation.one_default_audio:
        _compare_default_audio(built, problems)
    _compare_streams(original, built, kept, expectation, problems, notes)
    _compare_container(original, built, hashed > 0, problems, notes)
    _compare_chapters(original, built, expectation.chapters, problems, notes)
    _compare_tags(original, built, _identifier_map(original, built, kept), problems, notes)
    return Comparison(
        problems=tuple(problems),
        notes=tuple(notes),
        hashed_streams=hashed,
        label=expectation.label,
    )


def _compare_hashes(
    original: Evidence,
    built: Evidence,
    kept: Sequence[int],
    expectation: ExpectedDelta,
    problems: list[str],
    notes: list[str],
) -> int:
    if not original.hashed or not built.hashed:
        notes.append(
            "stream payloads were not hashed on both sides; this comparison covers "
            "the headers only, which is enough only after a header edit"
        )
        return 0
    expected = [original.hash_at(index) for index in kept]
    missing = [index for index, entry in zip(kept, expected, strict=True)
               if entry is None]
    if missing:
        problems.append(f"no hash was collected for original stream(s) {missing}")
        return 0
    got = list(built.hashes)
    allowed_extra = expectation.appended
    if len(got) != len(expected) + allowed_extra:
        problems.append(
            f"{len(expected) + allowed_extra} stream(s) expected in the new file "
            f"and {len(got)} are there"
        )
        return 0
    compared = 0
    for position, entry in enumerate(expected):
        assert entry is not None
        other = got[position]
        compared += 1
        if entry.value == other.value:
            continue
        where = f"position {position} (original stream {entry.index}, {entry.type})"
        if entry.type.startswith("v"):
            problems.append(
                f"the video payload differs at {where}; compare the frame hashes "
                "before calling it a re-encode, because a container that re-states "
                "its parameter sets changes these bytes and no picture"
            )
        else:
            problems.append(f"the payload differs at {where}")
    return compared


def _compare_tracks(
    original: Evidence,
    built: Evidence,
    kept: Sequence[int],
    expectation: ExpectedDelta,
    problems: list[str],
    notes: list[str],
) -> None:
    """The track table, position by position, for the tracks that had to survive.

    The identifiers the container uses and the stream indexes a decoder prints
    are the same numbers for this container, which is what lets one list of
    kept indexes drive both halves of the comparison.
    """
    old_tracks = [t for t in original.probe.tracks if t.id in set(kept)]
    new_tracks = list(built.probe.tracks)
    if len(new_tracks) < len(old_tracks):
        problems.append(
            f"{len(old_tracks)} track(s) had to survive and {len(new_tracks)} are there"
        )
        return
    fields = ("type", "codec_id", "language", "name", "channels", "sample_rate",
              "forced", "enabled", "default", "pixel_dimensions")
    may_change = expectation.may_change
    subtag_added = subtag_dropped = uid_changed = 0
    for old, new in zip(old_tracks, new_tracks, strict=False):
        for name in fields:
            if name in may_change:
                continue
            if getattr(old, name) != getattr(new, name):
                problems.append(
                    f"track {old.id}: {name} {getattr(old, name)!r} -> "
                    f"{getattr(new, name)!r}"
                )
        if old.language_ietf != new.language_ietf:
            if not old.language_ietf and new.language_ietf:
                subtag_added += 1
            elif old.language_ietf and not new.language_ietf:
                subtag_dropped += 1
            else:
                problems.append(
                    f"track {old.id}: two different modern language subtags, "
                    f"{old.language_ietf!r} -> {new.language_ietf!r}"
                )
        if old.uid != new.uid:
            uid_changed += 1
    if subtag_added:
        notes.append(
            f"{subtag_added} track(s) gained a modern language subtag; the legacy "
            "element, which is what most readers use, is unchanged"
        )
    if subtag_dropped:
        notes.append(
            f"{subtag_dropped} track(s) lost their modern language subtag; the "
            "legacy element is unchanged"
        )
    if uid_changed:
        notes.append(
            f"{uid_changed} track identifier(s) were regenerated; nothing outside "
            "the file references them and tags targeted at them are remapped"
        )
    if original.probe.attachments != built.probe.attachments:
        problems.append(
            f"the attachments changed: {len(original.probe.attachments)} -> "
            f"{len(built.probe.attachments)}"
        )


def _compare_default_audio(built: Evidence, problems: list[str]) -> None:
    """After a moved default, exactly one audio track may carry the flag."""
    flagged = [track.id for track in built.probe.audio if track.default]
    if len(flagged) != 1:
        problems.append(
            f"the default was moved and {len(flagged)} audio track(s) carry the "
            f"default flag {flagged}; exactly one should"
        )


def _compare_streams(
    original: Evidence,
    built: Evidence,
    kept: Sequence[int],
    expectation: ExpectedDelta,
    problems: list[str],
    notes: list[str],
) -> None:
    """What a player resolves: the language it will show, and where audio starts.

    This is the second half of the language check and the one that matters:
    the track header can say anything, and what a consumer reads is what the
    demuxer resolves. A caller that declared a language edit gets to change
    it; nobody else does.
    """
    old_streams = [s for s in original.probe.streams if s.index in set(kept)]
    new_streams = list(built.probe.streams)
    language_may_change = "language" in expectation.may_change
    for old, new in zip(old_streams, new_streams, strict=False):
        if old.language != new.language and not language_may_change:
            problems.append(
                f"stream {old.index}: the language a player resolves changed "
                f"{old.language_raw!r} -> {new.language_raw!r}"
            )
        if old.type != new.type or old.codec != new.codec:
            problems.append(
                f"stream {old.index}: {old.type}/{old.codec} -> {new.type}/{new.codec}"
            )
        if old.type == "audio":
            before, after = old.start_time_s, new.start_time_s
            if before is not None and after is not None:
                drift = abs(after - before)
                if drift > AUDIO_TIMING_TOLERANCE_S:
                    problems.append(
                        f"stream {old.index}: audio starts {drift * 1000:.1f} ms "
                        f"from where it did, past the {AUDIO_TIMING_TOLERANCE_S * 1000:.0f} ms bar"
                    )
                elif drift > 0:
                    notes.append(
                        f"stream {old.index}: audio start moved by {drift * 1000:.1f} ms"
                    )


def _compare_container(
    original: Evidence,
    built: Evidence,
    hashes_matched: bool,
    problems: list[str],
    notes: list[str],
) -> None:
    if (original.probe.container.title or "") != (built.probe.container.title or ""):
        problems.append(
            f"the container title changed: {original.probe.container.title!r} -> "
            f"{built.probe.container.title!r}"
        )
    if built.probe.has_cues is False:
        problems.append(
            "the new file has no seek index; every later seek becomes a read from "
            "the start of the file"
        )
    before = original.probe.container.duration_s
    after = built.probe.container.duration_s
    if before is None or after is None:
        return
    difference = after - before
    if abs(difference) <= DURATION_TOLERANCE_S:
        return
    if hashes_matched and difference < 0:
        notes.append(
            f"the container is {abs(difference):.3f} s shorter and every kept stream "
            "hash is identical: a dropped track ran past the rest, which is what "
            "removing it looks like"
        )
    else:
        problems.append(
            f"the container duration changed by {difference:+.3f} s"
        )


def _compare_chapters(
    original: Evidence,
    built: Evidence,
    replaced: chapters_xml.ChapterSet | None,
    problems: list[str],
    notes: list[str],
) -> None:
    """Count, start within a millisecond, and the name. Never the raw document.

    When the marks were replaced on purpose, the new file is held to the
    document that was written in rather than to the marks it replaced.
    """
    old = replaced if replaced is not None else original.chapters
    new = built.chapters
    if len(old) != len(new):
        problems.append(f"{len(old)} chapter mark(s) became {len(new)}")
        return
    tolerance = int(CHAPTER_TOLERANCE_S * 1_000_000_000)
    for position, (before, after) in enumerate(zip(old, new, strict=True), 1):
        if abs(before.start_ns - after.start_ns) > tolerance:
            problems.append(
                f"chapter {position} moved from {before.start_s:.3f} s to "
                f"{after.start_s:.3f} s"
            )
        if (before.name or "") != (after.name or ""):
            problems.append(
                f"chapter {position}: name {before.name!r} -> {after.name!r}"
            )
    if replaced is not None:
        if built.probe.edition_count > 1:
            problems.append(
                f"the new file has {built.probe.edition_count} chapter editions; "
                "the document was added beside the old marks instead of replacing them"
            )
    elif old and original.probe.edition_count != built.probe.edition_count:
        notes.append(
            f"the chapter edition count changed "
            f"{original.probe.edition_count} -> {built.probe.edition_count}"
        )


def _identifier_map(
    original: Evidence, built: Evidence, kept: Sequence[int]
) -> dict[int, int]:
    """Original track identifier -> the identifier it has in the new file.

    A rebuild regenerates them. Tags are keyed on the identifier, so without
    this translation every tag in every rebuilt file reads as lost.
    """
    old_tracks = [t for t in original.probe.tracks if t.id in set(kept)]
    mapping: dict[int, int] = {}
    for old, new in zip(old_tracks, built.probe.tracks, strict=False):
        if old.uid is not None and new.uid is not None:
            mapping[old.uid] = new.uid
    return mapping


def _translate(
    triples: Sequence[tuple[str, str, str]], mapping: dict[int, int]
) -> tuple[list[tuple[str, str, str]], int]:
    """Re-key the original's tags onto the new file's identifiers."""
    out: list[tuple[str, str, str]] = []
    orphaned = 0
    for key, name, value in triples:
        if not key:
            out.append((key, name, value))
            continue
        try:
            targets = [mapping[int(uid)] for uid in key.split("|")]
        except (KeyError, ValueError):
            orphaned += 1  # the track it was about is not in the new file
            continue
        out.append(("|".join(str(uid) for uid in sorted(targets)), name, value))
    return out, orphaned


def _compare_tags(
    original: Evidence,
    built: Evidence,
    mapping: dict[int, int],
    problems: list[str],
    notes: list[str],
) -> None:
    """Tags that say something, keyed on the identifiers the new file uses.

    Two classes are excluded and both exclusions are deliberate. Tags about a
    track that is not in the new file went with the track. Tags a muxer writes
    about its own output -- byte counts, frame counts, the writing
    application -- are recomputed on every rebuild, so comparing them measures
    the muxer.
    """
    translated, orphaned = _translate(original.tags.triples, mapping)
    before = {t for t in translated if t[1] not in ACCOUNTING_TAGS}
    after = {t for t in built.tags.triples if t[1] not in ACCOUNTING_TAGS}
    lost = sorted(before - after)
    gained = sorted(after - before)
    if lost:
        problems.append(f"{len(lost)} tag value(s) are gone: {lost[:3]}")
    if gained:
        notes.append(f"{len(gained)} tag value(s) are new: {gained[:3]}")
    if orphaned:
        notes.append(
            f"{orphaned} tag value(s) belonged to tracks that are not in the new "
            "file and went with them"
        )


def describe(evidence: Evidence) -> Iterable[str]:
    """A few lines about one side, for a log or a report."""
    yield f"{evidence.path.name}: {len(evidence.probe.tracks)} track(s)"
    yield f"  hashes: {len(evidence.hashes)}" if evidence.hashed else "  hashes: none"
    yield f"  chapters: {len(evidence.chapters)}"
    yield f"  tags: {len(evidence.tags)}"
