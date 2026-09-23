"""The element scan: the cheap question the identification output cannot answer.

The synthetic cases build the byte structure by hand, so the parsing is tested
without a media file and the damaged shapes -- truncated, unknown length, not
this format at all -- can be produced deliberately.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit import ebml

EBML_HEADER = 0x1A45DFA3
SEGMENT = 0x18538067
SEEKHEAD = 0x114D9B74
CUES = 0x1C53BB6B
TRACKS = 0x1654AE6B
INFO = 0x1549A966


def vint(value: int) -> bytes:
    """A length, written the way this format writes one."""
    for width in range(1, 9):
        limit = (1 << (7 * width)) - 1
        if value < limit:
            marker = 1 << (7 * width)
            return (marker | value).to_bytes(width, "big")
    raise ValueError(value)


def identifier(value: int) -> bytes:
    length = (value.bit_length() + 7) // 8
    return value.to_bytes(length, "big")


def element(eid: int, payload: bytes) -> bytes:
    return identifier(eid) + vint(len(payload)) + payload


def seek_entry(target: int, position: int) -> bytes:
    body = element(0x53AB, identifier(target)) + element(
        0x53AC, position.to_bytes(4, "big")
    )
    return element(0x4DBB, body)


def matroska(body: bytes, *, segment_length: int | None = None) -> bytes:
    head = element(EBML_HEADER, b"\x00" * 4)
    length = len(body) if segment_length is None else segment_length
    return head + identifier(SEGMENT) + vint(length) + body


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ the fast path
def test_a_seek_head_naming_the_index_answers_without_walking(tmp_path: Path) -> None:
    head = element(
        SEEKHEAD, seek_entry(CUES, 900) + seek_entry(TRACKS, 100)
    )
    path = write(tmp_path, "indexed.mkv", matroska(head + b"\x00" * 64))
    result = ebml.scan(path)
    assert result.status == "ok-seekhead"
    assert result.has_cues is True
    assert "Tracks" in result.names
    assert result.is_matroska


def test_a_chained_seek_head_is_followed(tmp_path: Path) -> None:
    """A muxer writes a small head at the front pointing at the full one."""
    second = element(SEEKHEAD, seek_entry(CUES, 4096))
    first_payload = seek_entry(SEEKHEAD, 0)  # patched below once the size is known
    first = element(SEEKHEAD, first_payload)
    second_position = len(first)
    first = element(SEEKHEAD, seek_entry(SEEKHEAD, second_position))
    body = first + second
    path = write(tmp_path, "chained.mkv", matroska(body))
    result = ebml.scan(path)
    assert result.has_cues is True
    assert result.status == "ok-seekhead"


def test_a_loop_between_seek_heads_terminates(tmp_path: Path) -> None:
    body = element(SEEKHEAD, seek_entry(SEEKHEAD, 0))
    path = write(tmp_path, "looping.mkv", matroska(body))
    result = ebml.scan(path)
    assert result.has_cues is False


# ------------------------------------------------------------------ the slow path
def test_a_file_with_no_seek_head_is_walked(tmp_path: Path) -> None:
    body = element(INFO, b"\x00" * 8) + element(TRACKS, b"\x00" * 8)
    path = write(tmp_path, "unindexed.mkv", matroska(body))
    result = ebml.scan(path)
    assert result.status == "ok-walk"
    assert result.names == frozenset({"Info", "Tracks"})
    assert result.has_cues is False


def test_the_walk_finds_the_index_when_no_seek_head_names_it(tmp_path: Path) -> None:
    body = element(TRACKS, b"\x00" * 4) + element(CUES, b"\x00" * 4)
    path = write(tmp_path, "walked.mkv", matroska(body))
    assert ebml.scan(path).has_cues is True


def test_an_unknown_element_is_reported_by_its_identifier(tmp_path: Path) -> None:
    body = element(0x1F43B675, b"\x00") + element(0xFF, b"\x00")
    path = write(tmp_path, "unknown.mkv", matroska(body))
    names = ebml.scan(path).names
    assert "Cluster" in names
    assert any(name.startswith("0x") for name in names)


# --------------------------------------------------------------- damaged and odd
def test_a_file_that_is_not_this_format_says_so(tmp_path: Path) -> None:
    path = write(tmp_path, "other.mkv", b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32)
    result = ebml.scan(path)
    assert result.status in {"not-ebml", "empty"}
    assert result.is_matroska is False
    assert result.names == frozenset()


def test_an_empty_file_is_not_an_error(tmp_path: Path) -> None:
    path = write(tmp_path, "empty.mkv", b"")
    assert ebml.scan(path).status == "empty"


def test_a_truncated_element_ends_the_walk_without_losing_what_was_read(
    tmp_path: Path,
) -> None:
    body = element(TRACKS, b"\x00" * 4) + identifier(CUES)
    path = write(tmp_path, "truncated.mkv", matroska(body, segment_length=len(body) + 40))
    result = ebml.scan(path)
    assert "Tracks" in result.names
    assert result.status == "truncated"


def test_a_segment_of_unknown_length_still_reports_what_it_found(
    tmp_path: Path,
) -> None:
    """A stream-written file declares no segment size; the walk stops at the end."""
    body = element(INFO, b"\x00" * 4)
    data = element(EBML_HEADER, b"\x00" * 4) + identifier(SEGMENT) + b"\xff" + body
    path = write(tmp_path, "streamed.mkv", data)
    result = ebml.scan(path)
    assert "Info" in result.names


def test_a_missing_file_raises_rather_than_answering_wrongly(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        ebml.scan(tmp_path / "does-not-exist.mkv")


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
def test_the_generated_fixtures_carry_an_index(media_fixtures: dict[str, Path]) -> None:
    result = ebml.scan(media_fixtures["chapter_grid.mkv"])
    assert result.has_cues is True
    assert "Chapters" in result.names or "Chapters" in result.indexed


@pytest.mark.needs_ffmpeg
def test_the_renamed_fixture_is_not_this_format(media_fixtures: dict[str, Path]) -> None:
    assert ebml.scan(media_fixtures["not_really_mkv.mkv"]).is_matroska is False
