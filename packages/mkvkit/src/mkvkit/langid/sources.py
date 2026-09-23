"""mkvkit.langid.sources -- where the list of tracks to look at comes from.

A language pass needs one thing before it can start: a list of
``(file, audio track)`` pairs with enough context to be worth asking about.
Two sources produce that, and which of them is the *default* is a design
decision rather than a detail.

**The filesystem walk is the default.** It needs a directory and ffprobe, and
nothing else -- no server, no account, no token. Anyone can run this package
against a folder of files, which is what makes it a tool rather than an
appendage of one particular setup.

**The catalogue adapter is optional.** A media server knows things the
filesystem does not: which items are episodes of the same season, what the
catalogue believes the original language is, how long the item runs without
decoding it. That is worth having, and it is worth having *behind an
interface*, because the moment it is the only way in, the package only works
for the people who run that server.

The adapter therefore takes any object that can answer an item query --
anything with an ``items(**params)`` method returning records, which the
server-side package's client satisfies without either package importing the
other. The dependency points one way, and it is not this way.

**Jobs are data, and they are written down.** A job list is a JSONL file: it
can be split across machines, filtered by hand, committed to a work directory
beside the results, and re-run a year later. Anything that can only exist
inside one process cannot be resumed, and a pass over a large library will be
interrupted.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from ..langcodes import canonical, is_unknown

__all__ = [
    "AUDIO_SUFFIXES",
    "SKIP_TITLE_HINTS",
    "CatalogueSource",
    "FilesystemSource",
    "ItemQuery",
    "Job",
    "JobSource",
    "device_hint",
    "read_jobs",
    "write_jobs",
]

log = logging.getLogger(__name__)

#: Containers worth probing. Anything else is either not media or has no
#: separable audio tracks to ask about.
AUDIO_SUFFIXES = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mka", ".webm", ".mpg"}
)

#: Track titles that say the track is not the programme's dialogue. Detecting
#: the language of a commentary track is not wrong, but it answers a different
#: question from the one a language pass is asking, and it is routinely a
#: different language from the film.
SKIP_TITLE_HINTS = (
    "commentary", "kommentar", "audio description", "audiodeskription",
    "descriptive", "described", "visually impaired", "karaoke", "isolated score",
)


@dataclass(frozen=True)
class Job:
    """One audio track to look at, with the context that helps decide about it.

    ``stream_index`` is the container's own index -- what a probe and a
    catalogue both report. ``audio_ord`` is the position among the audio
    streams only, which is what a decoder wants. Keeping both is not
    redundancy: confusing them is an off-by-one that silently reports the
    wrong track's language, and the two are equal often enough that the bug
    survives a casual test.
    """

    path: str
    stream_index: int
    audio_ord: int
    n_audio: int = 1
    tag: str | None = None
    codec: str | None = None
    channels: int | None = None
    title: str | None = None
    runtime_s: float | None = None
    item_id: str | None = None
    device: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.path, self.stream_index)

    @property
    def unknown(self) -> bool:
        """True when nothing in the file claims to know this track's language."""
        return is_unknown(self.tag)

    def to_record(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Job:
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in record.items() if k in fields})


class JobSource(Protocol):
    """Anything that can produce a list of tracks to look at."""

    def jobs(self) -> Iterator[Job]:
        ...


class ItemQuery(Protocol):
    """The one thing the catalogue adapter needs, and the only thing.

    Structural on purpose: the server-side client satisfies this without
    either package importing the other, and a test satisfies it with a list.
    """

    def items(self, **params: Any) -> Iterable[Mapping[str, Any]]:
        ...


def device_hint(path: str | os.PathLike[str]) -> str:
    """A coarse name for the storage this path lives on.

    Coarse is the point: it is used to keep one reader per physical device, and
    for that purpose the anchor of the path is a better answer than a correct
    one obtained by asking the operating system -- it is free, it works on both
    platforms, and being wrong about a network share costs nothing worse than a
    second reader.
    """
    text = str(path)
    # read the path as it is written rather than as this machine would read it:
    # a job list is routinely built on one platform and run on another
    pure: PurePath = PureWindowsPath(text) if "\\" in text else PurePosixPath(text)
    anchor = pure.anchor
    if anchor:
        return anchor.rstrip("\\/") or anchor
    return pure.parts[0] if pure.parts else "?"


# ------------------------------------------------------------------ filesystem
@dataclass
class FilesystemSource:
    """Walk directories, probe what is there, and emit one job per audio track.

    The default source. Needs a probe program and nothing else.
    """

    roots: Sequence[Path | str]
    ffprobe: str | os.PathLike[str] = "ffprobe"
    suffixes: frozenset[str] = AUDIO_SUFFIXES
    only_unknown: bool = False
    skip_titles: tuple[str, ...] = SKIP_TITLE_HINTS
    timeout_s: float = 120.0
    probe: Any = None  # injected in tests; defaults to running ffprobe

    def files(self) -> Iterator[Path]:
        for root in self.roots:
            base = Path(root)
            if base.is_file():
                yield base
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.suffix.lower() in self.suffixes:
                    yield path

    def jobs(self) -> Iterator[Job]:
        for path in self.files():
            try:
                probed = self._probe(path)
            except Exception as exc:
                log.warning("could not probe %s: %s", path.name, exc)
                continue
            yield from self._jobs_for(path, probed)

    def _jobs_for(self, path: Path, probed: Mapping[str, Any]) -> Iterator[Job]:
        streams = [
            s for s in probed.get("streams", []) if s.get("codec_type") == "audio"
        ]
        runtime = _float(probed.get("format", {}).get("duration"))
        for ordinal, stream in enumerate(streams):
            tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
            # A tag element and the track header both spell it "language", and
            # the probe reports the key in a different case depending on which
            # of them the value came from. Lower-casing the keys is the whole
            # fix, and not doing it reads as "no language at all".
            title = tags.get("title")
            if title and any(hint in title.lower() for hint in self.skip_titles):
                continue
            tag = canonical(tags.get("language"))
            if self.only_unknown and tag is not None:
                continue
            yield Job(
                path=str(path),
                stream_index=int(stream.get("index", ordinal)),
                audio_ord=ordinal,
                n_audio=len(streams),
                tag=tag,
                codec=stream.get("codec_name"),
                channels=_int(stream.get("channels")),
                title=title,
                runtime_s=runtime,
                device=device_hint(path),
            )

    def _probe(self, path: Path) -> Mapping[str, Any]:
        if self.probe is not None:
            result: Mapping[str, Any] = self.probe(path)
            return result
        command = [
            str(self.ffprobe), "-v", "error", "-select_streams", "a",
            "-show_streams", "-show_format", "-of", "json", str(path),
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=self.timeout_s, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip()[:300] or "probe failed")
        parsed: Mapping[str, Any] = json.loads(completed.stdout or "{}")
        return parsed


# ------------------------------------------------------------------- catalogue
@dataclass
class CatalogueSource:
    """Jobs from a media catalogue, for the context the filesystem cannot give.

    Optional by construction: it takes a query object rather than importing a
    client, so this package neither depends on a server nor knows which one it
    is talking to.

    Two things it must do that a naive version does not. It asks for the fields
    it needs *by name*, because a catalogue that is asked for everything
    returns a great deal of personal data that has no business in a language
    pass -- watch positions, favourites, who last played what. And it reports
    the audio ordinal separately from the container index, because the
    catalogue reports the latter and the decoder wants the former.
    """

    query: ItemQuery
    only_unknown: bool = False
    skip_titles: tuple[str, ...] = SKIP_TITLE_HINTS
    fields: str = "Path,MediaStreams,RunTimeTicks"
    include_types: str = "Movie,Episode"

    #: one hundred nanoseconds, which is the unit these catalogues count in
    TICKS_PER_SECOND = 10_000_000

    def jobs(self) -> Iterator[Job]:
        for item in self.query.items(
            recursive=True, includeItemTypes=self.include_types, fields=self.fields
        ):
            path = item.get("Path")
            if not path:
                continue  # a record with no file is not something to decode
            streams = [
                s for s in (item.get("MediaStreams") or [])
                if s.get("Type") == "Audio"
            ]
            ticks = item.get("RunTimeTicks")
            runtime = float(ticks) / self.TICKS_PER_SECOND if ticks else None
            for ordinal, stream in enumerate(streams):
                title = stream.get("Title")
                if title and any(hint in title.lower() for hint in self.skip_titles):
                    continue
                tag = canonical(stream.get("Language"))
                if self.only_unknown and tag is not None:
                    continue
                yield Job(
                    path=str(path),
                    stream_index=int(stream.get("Index", ordinal)),
                    audio_ord=ordinal,
                    n_audio=len(streams),
                    tag=tag,
                    codec=stream.get("Codec"),
                    channels=_int(stream.get("Channels")),
                    title=title,
                    runtime_s=runtime,
                    item_id=item.get("Id"),
                    device=device_hint(str(path)),
                )


# ------------------------------------------------------------------- job files
def write_jobs(path: Path | str, jobs: Iterable[Job]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for job in jobs:
            handle.write(json.dumps(job.to_record(), ensure_ascii=False) + "\n")
            written += 1
    log.info("%d job(s) written to %s", written, target)
    return written


def read_jobs(path: Path | str) -> Iterator[Job]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield Job.from_record(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError):
                log.warning("%s:%d is not a usable job, skipping", path, number)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
