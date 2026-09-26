"""Putting the rebuild in place: the parking, the checks, and the restore.

These run on ordinary files in a temporary directory -- the module moves and
copies bytes and asks a check about the result, and the check is an argument.
So the dangerous minute is tested exactly, including the failures, without a
media file anywhere near it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.integrity import IntegrityReport
from mkvkit.run import CommandFailed, Result
from mkvkit.swap import SwapPair, parked_path, summarise, swap, swap_all

from tests.stand_ins import payload_is_a_stand_in


@pytest.fixture(autouse=True)
def _replacements_are_stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """These files are bytes, not media; the payload tests are at the end."""
    payload_is_a_stand_in(monkeypatch)


def tree(tmp_path: Path, *, name: str = "example.mkv") -> tuple[Path, Path, Path]:
    live = tmp_path / "media" / "movies" / "Example (1998)"
    staging = tmp_path / "staging"
    parked = tmp_path / "parked"
    live.mkdir(parents=True)
    staging.mkdir()
    (live / name).write_bytes(b"the original file")
    (staging / name).write_bytes(b"the rebuilt file, a different size")
    return live / name, staging / name, parked


def accept(_path: Path) -> list[str]:
    return []


def refuse(_path: Path) -> list[str]:
    return ["the file that arrived is not what was expected"]


# --------------------------------------------------------------------- the path
def test_the_parked_copy_keeps_the_layout_it_came_from(tmp_path: Path) -> None:
    """A directory of hundreds of files with one name is not a backup."""
    keeper = Path("/srv/media/movies/Example (1998)/example.mkv")
    destination = parked_path(keeper, Path("/srv/parked"))
    assert destination.name == "example.mkv"
    assert "Example (1998)" in destination.parts
    assert destination.parts[:2] == Path("/srv/parked").parts[:2]


# ------------------------------------------------------------------- refusals
def test_a_swap_never_renames(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)
    other = replacement.with_name("something else.mkv")
    replacement.rename(other)
    result = swap(SwapPair(keeper, other), parked_dir=parked, dry_run=False)
    assert not result.ok
    assert "never renames" in result.problems[0]
    assert keeper.read_bytes() == b"the original file"


def test_a_missing_replacement_stops_it(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)
    replacement.unlink()
    result = swap(SwapPair(keeper, replacement), parked_dir=parked, dry_run=False)
    assert not result.ok
    assert keeper.is_file()


def test_something_already_parked_is_never_overwritten(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)
    destination = parked_path(keeper, parked)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"an earlier original")
    result = swap(SwapPair(keeper, replacement), parked_dir=parked, dry_run=False)
    assert not result.ok
    assert "already parked" in result.problems[0]
    assert destination.read_bytes() == b"an earlier original"


# --------------------------------------------------------------------- dry run
def test_a_dry_run_moves_nothing_and_says_where_it_would_park(
    tmp_path: Path,
) -> None:
    keeper, replacement, parked = tree(tmp_path)
    result = swap(SwapPair(keeper, replacement), parked_dir=parked)
    assert not result.applied
    assert result.ok
    assert result.parked is not None
    assert keeper.read_bytes() == b"the original file"
    assert not parked.exists()


# --------------------------------------------------------------------- the swap
def test_the_original_is_parked_and_the_rebuild_takes_its_path(
    tmp_path: Path,
) -> None:
    keeper, replacement, parked = tree(tmp_path)
    result = swap(
        SwapPair(keeper, replacement), parked_dir=parked, dry_run=False, check=accept
    )
    assert result.applied, result.problems
    assert keeper.read_bytes() == b"the rebuilt file, a different size"
    assert result.parked is not None
    assert result.parked.read_bytes() == b"the original file"
    assert replacement.is_file()  # the staged file is not consumed


def test_a_failed_check_puts_the_original_back(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)
    result = swap(
        SwapPair(keeper, replacement), parked_dir=parked, dry_run=False, check=refuse
    )
    assert not result.ok
    assert "put back" in result.problems[-1]
    assert keeper.read_bytes() == b"the original file"
    assert not parked_path(keeper, parked).exists()


@pytest.mark.parametrize(
    "interruption",
    [
        CommandFailed(Result("mkvmerge", ("mkvmerge", "-J"), 2, "", "cannot open")),
        KeyboardInterrupt(),
    ],
    ids=["the-check-raised", "ctrl-c"],
)
def test_a_check_that_cannot_finish_puts_the_original_back(
    tmp_path: Path, interruption: BaseException
) -> None:
    """A check that raised verified nothing, so the arrived file must not stay."""
    keeper, replacement, parked = tree(tmp_path)

    def blow_up(_path: Path) -> list[str]:
        raise interruption

    with pytest.raises(type(interruption)):
        swap(
            SwapPair(keeper, replacement), parked_dir=parked, dry_run=False,
            check=blow_up,
        )
    assert keeper.read_bytes() == b"the original file"
    assert not parked_path(keeper, parked).exists()
    assert replacement.is_file()


def test_the_check_reads_the_file_at_its_destination(tmp_path: Path) -> None:
    """Not the staged copy: the question is what is in place now."""
    keeper, replacement, parked = tree(tmp_path)
    seen: list[Path] = []

    def record(path: Path) -> list[str]:
        seen.append(path)
        return []

    swap(SwapPair(keeper, replacement), parked_dir=parked, dry_run=False, check=record)
    assert seen == [keeper]


def test_a_size_comparison_would_have_rejected_this_swap(tmp_path: Path) -> None:
    """A rebuilt file legitimately has a different size; size says nothing here."""
    keeper, replacement, parked = tree(tmp_path)
    original_size = keeper.stat().st_size
    swap(SwapPair(keeper, replacement), parked_dir=parked, dry_run=False, check=accept)
    assert keeper.stat().st_size != original_size


# -------------------------------------------------------------------- batches
def test_a_batch_stops_at_the_first_problem(tmp_path: Path) -> None:
    first_keeper, first_replacement, parked = tree(tmp_path, name="one.mkv")
    second_keeper = first_keeper.with_name("two.mkv")
    second_keeper.write_bytes(b"another original")
    second_replacement = first_replacement.with_name("two.mkv")
    second_replacement.write_bytes(b"another rebuild")
    third_keeper = first_keeper.with_name("three.mkv")
    third_keeper.write_bytes(b"a third original")
    third_replacement = first_replacement.with_name("three.mkv")
    third_replacement.write_bytes(b"a third rebuild")

    def only_the_first(path: Path) -> list[str]:
        return [] if path.name == "one.mkv" else ["something is wrong"]

    results = swap_all(
        [
            SwapPair(first_keeper, first_replacement),
            SwapPair(second_keeper, second_replacement),
            SwapPair(third_keeper, third_replacement),
        ],
        parked_dir=parked,
        dry_run=False,
        check=only_the_first,
    )
    assert len(results) == 2
    assert results[0].applied
    assert not results[1].ok
    assert second_keeper.read_bytes() == b"another original"
    assert third_keeper.read_bytes() == b"a third original"
    assert "1 swapped" in summarise(results)


def test_a_batch_can_be_told_to_carry_on(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path, name="one.mkv")
    second_keeper = keeper.with_name("two.mkv")
    second_keeper.write_bytes(b"another original")
    second_replacement = replacement.with_name("two.mkv")
    second_replacement.write_bytes(b"another rebuild")

    def only_the_second(path: Path) -> list[str]:
        return ["something is wrong"] if path.name == "one.mkv" else []

    results = swap_all(
        [SwapPair(keeper, replacement), SwapPair(second_keeper, second_replacement)],
        parked_dir=parked,
        dry_run=False,
        stop_on_problem=False,
        check=only_the_second,
    )
    assert len(results) == 2
    assert not results[0].ok
    assert results[1].applied
    assert keeper.read_bytes() == b"the original file"


def test_a_result_prints_as_something_a_person_can_read(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)
    text = str(swap(SwapPair(keeper, replacement), parked_dir=parked))
    assert "not swapped" in text
    assert "parked at" in text


# ------------------------------------------------ the replacement must play
def test_a_replacement_whose_payload_is_not_there_is_refused_before_anything_moves(
    tmp_path: Path,
) -> None:
    keeper, replacement, parked = tree(tmp_path)

    def empty_inside(path: Path) -> IntegrityReport:
        return IntegrityReport(path=path, problems=("14 of 16 sampled blocks are zeros",))

    for dry_run in (True, False):
        result = swap(SwapPair(keeper, replacement), parked_dir=parked,
                      dry_run=dry_run, check=accept, payload_check=empty_inside)
        assert not result.ok and "payload is not there" in result.problems[0]
    assert keeper.read_bytes() == b"the original file"
    assert not parked.exists()


def test_no_evidence_about_the_replacement_is_a_refusal(tmp_path: Path) -> None:
    keeper, replacement, parked = tree(tmp_path)

    def unmeasurable(path: Path) -> IntegrityReport:
        return IntegrityReport(path=path, evidence=False, problems=("ffprobe is missing",))

    def raising(path: Path) -> IntegrityReport:
        raise OSError("the disk went away")

    for check in (unmeasurable, raising):
        result = swap(SwapPair(keeper, replacement), parked_dir=parked,
                      dry_run=False, check=accept, payload_check=check)
        assert not result.ok and "no evidence" in result.problems[0]
    assert keeper.read_bytes() == b"the original file"


@pytest.mark.needs_ffmpeg
def test_a_zero_filled_rebuild_is_refused_by_the_real_check(
    tmp_path: Path, media_fixtures: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real check, on real media: a rebuild that stopped half way."""
    monkeypatch.undo()
    source = media_fixtures["tiny_multitrack.mkv"]
    keeper = tmp_path / "live" / "Northwind - S01E03.mkv"
    replacement = tmp_path / "staging" / "Northwind - S01E03.mkv"
    keeper.parent.mkdir()
    replacement.parent.mkdir()
    keeper.write_bytes(source.read_bytes())
    data = bytearray(source.read_bytes())
    data[32 << 10:len(data) - (16 << 10)] = bytes(len(data) - (48 << 10))
    replacement.write_bytes(bytes(data))
    result = swap(SwapPair(keeper, replacement), parked_dir=tmp_path / "parked",
                  dry_run=False, check=accept)
    assert not result.ok and "payload is not there" in result.problems[0]
    assert keeper.read_bytes() == source.read_bytes()
