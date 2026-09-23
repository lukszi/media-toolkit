"""mkvkit.ebml -- which top-level elements a Matroska file carries, cheaply.

One question is asked here and it is worth a module: *does this file have a
seek index, chapters, tags, attachments?* The identification output does not
answer it, and the answer decides real behaviour -- a container without an
index makes every seek a read from byte zero, which is the difference between
sampling a handful of windows in seconds and in minutes.

The file is read, never decoded. Two paths:

**The index itself.** A muxer writes a small seek head at the front of the
segment, often pointing at a full one at the end. Following at most four of
them answers the question with a handful of reads at known offsets, whatever
the size of the file.

**Walking the segment.** Only when no seek head names the index: step over
every top-level element by its declared size. On a file of many gigabytes that
is thousands of seeks, so it is the fallback and never the first try.

Both paths are defensive by construction. A truncated or damaged file returns
what was readable with a status saying where it stopped, because this runs
over whole collections and one unreadable file must not end the pass.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

__all__ = [
    "ELEMENT_NAMES",
    "ElementScan",
    "scan",
]

# Element identifiers, as they are written on disk (with the length marker).
_EBML_HEADER: Final = 0x1A45DFA3
_SEGMENT: Final = 0x18538067
_SEEKHEAD: Final = 0x114D9B74
_CUES: Final = 0x1C53BB6B
_CHAPTERS: Final = 0x1043A770
_ATTACHMENTS: Final = 0x1941A469
_TAGS: Final = 0x1254C367
_TRACKS: Final = 0x1654AE6B
_INFO: Final = 0x1549A966
_CLUSTER: Final = 0x1F43B675
_SEEK: Final = 0x4DBB
_SEEK_ID: Final = 0x53AB
_SEEK_POSITION: Final = 0x53AC

#: Identifier -> the name this package reports.
ELEMENT_NAMES: Final[dict[int, str]] = {
    _INFO: "Info",
    _TRACKS: "Tracks",
    _CUES: "Cues",
    _CLUSTER: "Cluster",
    _CHAPTERS: "Chapters",
    _ATTACHMENTS: "Attachments",
    _TAGS: "Tags",
    _SEEKHEAD: "SeekHead",
    0xEC: "Void",
    0xBF: "CRC-32",
}

#: An all-ones length means "unknown", and it is not a size to skip over.
_UNKNOWN_LENGTH: Final = (1 << 56) - 1
#: A seek head larger than this is not a seek head.
_MAX_SEEKHEAD_BYTES: Final = 4 << 20
#: How many chained seek heads are followed before giving up.
_MAX_SEEKHEAD_CHAIN: Final = 4


@dataclass(frozen=True)
class ElementScan:
    """What the scan found, and how far it got.

    ``present`` holds elements seen directly, ``indexed`` those a seek head
    names. The union is what a caller normally wants: an element the index
    points at is in the file whether or not the walk reached it.
    """

    present: frozenset[str] = frozenset()
    indexed: frozenset[str] = frozenset()
    status: str = "ok"

    @property
    def names(self) -> frozenset[str]:
        return self.present | self.indexed

    @property
    def has_cues(self) -> bool:
        return "Cues" in self.names

    @property
    def is_matroska(self) -> bool:
        """Whether the file begins the way a Matroska file has to begin."""
        return self.status not in {"not-ebml", "no-segment", "empty"}


def scan(path: Path | str) -> ElementScan:
    """Read the top-level structure of one file."""
    target = Path(path)
    size = target.stat().st_size
    with target.open("rb") as handle:
        identifier = _read_vint(handle, keep_marker=True)
        if identifier is None:
            return ElementScan(status="empty")
        if identifier != _EBML_HEADER:
            return ElementScan(status="not-ebml")
        header_length = _read_vint(handle, keep_marker=False)
        if header_length is None:
            return ElementScan(status="not-ebml")
        handle.seek(header_length, 1)
        identifier = _read_vint(handle, keep_marker=True)
        if identifier != _SEGMENT:
            return ElementScan(status="no-segment")
        declared = _read_vint(handle, keep_marker=False)
        start = handle.tell()
        end = start + declared if declared and declared < _UNKNOWN_LENGTH else size

        indexed, found_cues = _follow_seek_heads(handle, start)
        if found_cues:
            return ElementScan(
                present=frozenset({"SeekHead"}), indexed=indexed, status="ok-seekhead"
            )
        present, status = _walk(handle, start, end)
        return ElementScan(present=present, indexed=indexed, status=status)


# ------------------------------------------------------------------- the paths
def _follow_seek_heads(
    handle: BinaryIO, segment_start: int
) -> tuple[frozenset[str], bool]:
    """Names the index promises, and whether it promises the seek index itself."""
    names: set[str] = set()
    visited: set[int] = set()
    position = segment_start
    for _ in range(_MAX_SEEKHEAD_CHAIN):
        handle.seek(position)
        if _read_vint(handle, keep_marker=True) != _SEEKHEAD:
            return frozenset(names), False
        length = _read_vint(handle, keep_marker=False)
        if length is None or length > _MAX_SEEKHEAD_BYTES:
            return frozenset(names), False
        entries = _parse_seek_head(handle.read(length))
        names.update(ELEMENT_NAMES[i] for i in entries if i in ELEMENT_NAMES)
        if _CUES in entries:
            return frozenset(names), True
        following = entries.get(_SEEKHEAD)
        if following is None or following in visited:
            return frozenset(names), False
        visited.add(following)
        position = segment_start + following
    return frozenset(names), False


def _walk(handle: BinaryIO, start: int, end: int) -> tuple[frozenset[str], str]:
    """Step over every top-level element by its declared size."""
    names: set[str] = set()
    handle.seek(start)
    while handle.tell() < end:
        identifier = _read_vint(handle, keep_marker=True)
        if identifier is None:
            return frozenset(names), "truncated"
        length = _read_vint(handle, keep_marker=False)
        if length is None:
            return frozenset(names), "truncated"
        names.add(ELEMENT_NAMES.get(identifier, hex(identifier)))
        if length >= _UNKNOWN_LENGTH:
            # An element of unknown length can only be stepped over by parsing
            # its children, which is a decode. Report what is known instead.
            return frozenset(names), "ok-walk-partial"
        handle.seek(length, 1)
    return frozenset(names), "ok-walk"


def _parse_seek_head(data: bytes) -> dict[int, int]:
    """Element identifier -> position relative to the start of the segment."""
    out: dict[int, int] = {}
    offset = 0
    while offset < len(data):
        identifier, offset = _buffer_vint(data, offset, keep_marker=True)
        if identifier is None:
            break
        length, offset = _buffer_vint(data, offset, keep_marker=False)
        if length is None:
            break
        if identifier == _SEEK:
            entry = data[offset : offset + length]
            seek_id, seek_position = _parse_seek(entry)
            if seek_id is not None and seek_position is not None:
                out.setdefault(seek_id, seek_position)
        offset += length
    return out


def _parse_seek(entry: bytes) -> tuple[int | None, int | None]:
    identifier: int | None = None
    position: int | None = None
    offset = 0
    while offset < len(entry):
        key, offset = _buffer_vint(entry, offset, keep_marker=True)
        if key is None:
            break
        length, offset = _buffer_vint(entry, offset, keep_marker=False)
        if length is None:
            break
        value = entry[offset : offset + length]
        if key == _SEEK_ID and value:
            identifier = int.from_bytes(value, "big")
        elif key == _SEEK_POSITION and value:
            position = int.from_bytes(value, "big")
        offset += length
    return identifier, position


# ----------------------------------------------------------- variable integers
def _width(first: int) -> int | None:
    """How many bytes this variable-length integer occupies, from its first byte."""
    if first == 0:
        return None
    width, mask = 1, 0x80
    while not first & mask:
        mask >>= 1
        width += 1
        if width > 8:
            return None
    return width


def _read_vint(handle: BinaryIO, *, keep_marker: bool) -> int | None:
    """Read one variable-length integer from the file.

    ``keep_marker`` distinguishes the two uses: an element identifier is the
    bytes as written, a length has its leading marker bit removed.
    """
    head = handle.read(1)
    if not head:
        return None
    width = _width(head[0])
    if width is None:
        return None
    rest = handle.read(width - 1)
    if len(rest) != width - 1:
        return None
    value = head[0] if keep_marker else head[0] & ((0x80 >> (width - 1)) - 1)
    for byte in rest:
        value = (value << 8) | byte
    return value


def _buffer_vint(
    data: bytes, offset: int, *, keep_marker: bool
) -> tuple[int | None, int]:
    """The same, from a buffer; returns the value and the offset after it."""
    if offset >= len(data):
        return None, offset
    first = data[offset]
    width = _width(first)
    if width is None or offset + width > len(data):
        return None, offset
    value = first if keep_marker else first & ((0x80 >> (width - 1)) - 1)
    for byte in data[offset + 1 : offset + width]:
        value = (value << 8) | byte
    return value, offset + width
