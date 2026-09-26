"""The fixture cache: built once, trusted only when complete, shared safely.

None of these runs a media program. The generator is replaced by one that
writes placeholder files and counts how often it was asked, which is the
property under test: parallel test processes on a cold cache build one set
between them, and a set that was never finished is never handed out.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tests.fixtures import make_fixtures


@pytest.fixture
def generated(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every directory the stand-in generator was asked to fill."""
    calls: list[Path] = []

    def fake_generate(directory: Path) -> list[str]:
        calls.append(directory)
        time.sleep(0.05)  # long enough for a second builder to arrive
        for name in make_fixtures.NAMES:
            (directory / name).write_bytes(b"x")
        return list(make_fixtures.NAMES)

    monkeypatch.setattr(make_fixtures, "_generate", fake_generate)
    monkeypatch.setattr(make_fixtures, "ffmpeg_missing", lambda: None)
    monkeypatch.setattr(make_fixtures, "cache_key", lambda: "k" * 20)
    return calls


def test_parallel_builders_on_a_cold_cache_build_once(
    tmp_path: Path, generated: list[Path]
) -> None:
    results: list[dict[str, Path]] = []
    errors: list[BaseException] = []

    def one() -> None:
        try:
            results.append(make_fixtures.build(tmp_path))
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(generated) == 1
    assert all(result == results[0] for result in results)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".lock", "k" * 20]


def test_a_cached_set_is_returned_without_building(
    tmp_path: Path, generated: list[Path]
) -> None:
    first = make_fixtures.build(tmp_path)
    second = make_fixtures.build(tmp_path)
    assert first == second
    assert len(generated) == 1


def test_a_set_without_its_manifest_is_not_trusted(
    tmp_path: Path, generated: list[Path]
) -> None:
    """A directory under the final name that was never finished is built again."""
    half = tmp_path / ("k" * 20)
    half.mkdir()
    (half / "tiny_multitrack.mkv").write_bytes(b"half")
    built = make_fixtures.build(tmp_path)
    assert len(generated) == 1
    assert built["tiny_multitrack.mkv"].read_bytes() == b"x"


def test_a_set_missing_a_file_it_lists_is_not_trusted(
    tmp_path: Path, generated: list[Path]
) -> None:
    make_fixtures.build(tmp_path)
    (tmp_path / ("k" * 20) / "offset_pair.mka").unlink()
    make_fixtures.build(tmp_path)
    assert len(generated) == 2


def test_an_interrupted_build_leaves_nothing_the_next_run_trusts(
    tmp_path: Path, generated: list[Path]
) -> None:
    leftover = tmp_path / ".building-interrupted"
    leftover.mkdir()
    (leftover / "sample_cues.srt").write_bytes(b"partial")
    make_fixtures.build(tmp_path)
    assert not leftover.exists()


def test_force_builds_again(tmp_path: Path, generated: list[Path]) -> None:
    make_fixtures.build(tmp_path)
    make_fixtures.build(tmp_path, force=True)
    assert len(generated) == 2
    manifest = tmp_path / ("k" * 20) / make_fixtures.MANIFEST
    assert json.loads(manifest.read_text(encoding="utf-8")) == list(make_fixtures.NAMES)


def test_the_root_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(make_fixtures.CACHE_VARIABLE, str(tmp_path / "elsewhere"))
    assert make_fixtures.cache_root() == (tmp_path / "elsewhere").resolve()
    monkeypatch.delenv(make_fixtures.CACHE_VARIABLE)
    assert make_fixtures.cache_root() == make_fixtures.FIXTURE_DIR


def test_the_key_follows_the_programs_that_encode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A different encoder is a different set, not a stale one reused."""
    monkeypatch.setattr(make_fixtures, "_identity", lambda program: f"{program}=one")
    before = make_fixtures.cache_key()
    assert make_fixtures.cache_key() == before
    monkeypatch.setattr(make_fixtures, "_identity", lambda program: f"{program}=two")
    assert make_fixtures.cache_key() != before


def test_clean_removes_sets_and_an_older_flat_layout(
    tmp_path: Path, generated: list[Path]
) -> None:
    make_fixtures.build(tmp_path)
    (tmp_path / "tiny_multitrack.mkv").write_bytes(b"from before the cache")
    assert make_fixtures.clean(tmp_path) == 2
    assert [p.name for p in tmp_path.iterdir()] == [".lock"]
