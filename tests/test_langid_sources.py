"""Where the list of tracks comes from, and what the two sources agree about.

The filesystem source is the default and needs nothing but a directory. The
catalogue source is optional and takes any object that can answer an item
query, so this package neither imports a client nor knows which server it is
talking to -- which is what keeps the optional thing optional.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from mkvkit.langid.sources import (
    CatalogueSource,
    FilesystemSource,
    Job,
    device_hint,
    read_jobs,
    write_jobs,
)


def _probe(streams: list[dict[str, Any]], duration: str = "2700.0") -> dict[str, Any]:
    return {"streams": streams, "format": {"duration": duration}}


def _stream(
    index: int, language: str | None = None, *, title: str | None = None,
    upper_case_key: bool = False, codec: str = "ac3", channels: int = 6,
) -> dict[str, Any]:
    tags: dict[str, str] = {}
    if language is not None:
        tags["LANGUAGE" if upper_case_key else "language"] = language
    if title is not None:
        tags["title"] = title
    return {
        "index": index, "codec_type": "audio", "codec_name": codec,
        "channels": channels, "tags": tags,
    }


def _tree(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "series"
    root.mkdir()
    for name in names:
        (root / name).write_bytes(b"not really a container")
    return root


# ------------------------------------------------------------------ filesystem
def test_one_job_per_audio_track(tmp_path: Path) -> None:
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    source = FilesystemSource(
        roots=[root],
        probe=lambda _p: _probe([_stream(1, "eng"), _stream(2, "deu")]),
    )
    jobs = list(source.jobs())
    assert [job.audio_ord for job in jobs] == [0, 1]
    assert [job.stream_index for job in jobs] == [1, 2]
    assert [job.tag for job in jobs] == ["eng", "deu"]
    assert all(job.n_audio == 2 for job in jobs)
    assert all(job.runtime_s == 2700.0 for job in jobs)


def test_the_container_index_and_the_audio_ordinal_are_both_kept(
    tmp_path: Path
) -> None:
    """Confusing them reports the wrong track's language, and they are often equal."""
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    source = FilesystemSource(
        roots=[root], probe=lambda _p: _probe([_stream(3, "eng"), _stream(4, "deu")])
    )
    jobs = list(source.jobs())
    assert [(job.stream_index, job.audio_ord) for job in jobs] == [(3, 0), (4, 1)]


def test_a_language_from_a_tag_element_is_found_whatever_its_case(
    tmp_path: Path
) -> None:
    """A probe reports the key in upper case when it came from a tag element.

    Comparing case-sensitively reads as "no language at all", which is the
    difference between a track that is tagged and a track that is not.
    """
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    source = FilesystemSource(
        roots=[root], probe=lambda _p: _probe([_stream(1, "ger", upper_case_key=True)])
    )
    assert next(iter(source.jobs())).tag == "deu"


def test_only_unknown_skips_the_tracks_that_already_claim_a_language(
    tmp_path: Path
) -> None:
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    source = FilesystemSource(
        roots=[root], only_unknown=True,
        probe=lambda _p: _probe([_stream(1, "eng"), _stream(2, "und"), _stream(3)]),
    )
    jobs = list(source.jobs())
    assert [job.audio_ord for job in jobs] == [1, 2]
    assert all(job.unknown for job in jobs)


def test_tracks_that_are_not_the_programme_are_skipped(tmp_path: Path) -> None:
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    source = FilesystemSource(
        roots=[root],
        probe=lambda _p: _probe([
            _stream(1, "eng"),
            _stream(2, "eng", title="Director's Commentary"),
            _stream(3, "eng", title="Audio Description"),
        ]),
    )
    assert [job.audio_ord for job in list(source.jobs())] == [0]


def test_only_containers_worth_probing_are_walked(tmp_path: Path) -> None:
    root = _tree(
        tmp_path, "Northwind - S01E03.mkv", "Northwind - S01E03.nfo",
        "Northwind - S01E03.srt", "Northwind - S01E04.mp4",
    )
    source = FilesystemSource(roots=[root], probe=lambda _p: _probe([_stream(1, "eng")]))
    assert {Path(job.path).suffix for job in source.jobs()} == {".mkv", ".mp4"}


def test_a_file_that_cannot_be_probed_does_not_end_the_walk(tmp_path: Path) -> None:
    root = _tree(tmp_path, "a.mkv", "b.mkv")

    def probe(path: Path) -> dict[str, Any]:
        if path.name == "a.mkv":
            raise RuntimeError("that is not a container")
        return _probe([_stream(1, "eng")])

    jobs = list(FilesystemSource(roots=[root], probe=probe).jobs())
    assert [Path(job.path).name for job in jobs] == ["b.mkv"]


def test_a_single_file_is_a_valid_root(tmp_path: Path) -> None:
    root = _tree(tmp_path, "Northwind - S01E03.mkv")
    one = root / "Northwind - S01E03.mkv"
    source = FilesystemSource(roots=[one], probe=lambda _p: _probe([_stream(1, "eng")]))
    assert len(list(source.jobs())) == 1


def test_the_walk_is_ordered_so_two_runs_produce_the_same_list(
    tmp_path: Path
) -> None:
    root = _tree(tmp_path, "c.mkv", "a.mkv", "b.mkv")
    source = FilesystemSource(roots=[root], probe=lambda _p: _probe([_stream(1, "eng")]))
    first = [job.path for job in source.jobs()]
    assert first == sorted(first)


# ------------------------------------------------------------------- catalogue
class FakeCatalogue:
    """Anything with this one method is a catalogue as far as this package cares."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.asked: list[dict[str, Any]] = []

    def items(self, **params: Any) -> Iterable[Mapping[str, Any]]:
        self.asked.append(params)
        return list(self.rows)


def test_the_catalogue_source_asks_for_the_fields_it_needs_and_no_others() -> None:
    """A query for everything comes back carrying other people's viewing history."""
    catalogue = FakeCatalogue([])
    list(CatalogueSource(query=catalogue).jobs())
    assert catalogue.asked[0]["fields"] == "Path,MediaStreams,RunTimeTicks"
    assert catalogue.asked[0]["recursive"] is True


def test_the_catalogue_source_produces_the_same_job_shape() -> None:
    catalogue = FakeCatalogue([
        {
            "Id": "00000000-0000-0000-0000-000000000001",
            "Path": "/srv/media/series/Northwind - S01E03.mkv",
            "RunTimeTicks": 27_000_000_000,
            "MediaStreams": [
                {"Type": "Video", "Index": 0},
                {"Type": "Audio", "Index": 1, "Language": "eng", "Codec": "ac3",
                 "Channels": 6},
                {"Type": "Audio", "Index": 2, "Language": "ger", "Codec": "aac",
                 "Channels": 2},
            ],
        }
    ])
    jobs = list(CatalogueSource(query=catalogue).jobs())
    assert [(job.stream_index, job.audio_ord, job.tag) for job in jobs] == [
        (1, 0, "eng"), (2, 1, "deu")
    ]
    assert jobs[0].runtime_s == 2700.0
    assert jobs[0].item_id == "00000000-0000-0000-0000-000000000001"


def test_a_catalogue_row_with_no_file_is_skipped() -> None:
    catalogue = FakeCatalogue([
        {"Id": "00000000-0000-0000-0000-000000000002", "MediaStreams": []},
    ])
    assert list(CatalogueSource(query=catalogue).jobs()) == []


# ------------------------------------------------------------------- job files
def test_jobs_round_trip_through_a_file(tmp_path: Path) -> None:
    """A job list that only exists in a process cannot be resumed or split."""
    jobs = [
        Job(path="/srv/media/series/Northwind - S01E03.mkv", stream_index=1,
            audio_ord=0, n_audio=2, tag="eng", runtime_s=2700.0),
        Job(path="/srv/media/series/Northwind - S01E03.mkv", stream_index=2,
            audio_ord=1, n_audio=2),
    ]
    path = tmp_path / "jobs.jsonl"
    assert write_jobs(path, jobs) == 2
    assert list(read_jobs(path)) == jobs


def test_an_unusable_line_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        json.dumps({"path": "/srv/media/a.mkv", "stream_index": 1, "audio_ord": 0})
        + "\nnot json at all\n",
        encoding="utf-8",
    )
    assert len(list(read_jobs(path))) == 1


def test_an_unknown_field_in_a_job_file_is_ignored(tmp_path: Path) -> None:
    """A job list written by a later version still reads here."""
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        json.dumps({
            "path": "/srv/media/a.mkv", "stream_index": 1, "audio_ord": 0,
            "something_new": 42,
        }) + "\n",
        encoding="utf-8",
    )
    assert next(iter(read_jobs(path))).stream_index == 1


# ---------------------------------------------------------------- device hints
def test_the_device_hint_is_coarse_on_purpose() -> None:
    """It exists to keep one reader per device, not to be a correct answer."""
    assert device_hint("/srv/media/series/a.mkv") == "/"
    assert device_hint("C:" + "\\" + "Media" + "\\" + "a.mkv").endswith(":")
    assert device_hint("relative/a.mkv") == "relative"
