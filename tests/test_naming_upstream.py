"""The naming port against upstream's own naming tests, translated.

Three tables under ``tests/fixtures`` carry Jellyfin 12.1's episode, season
folder and extras test cases, each row citing the upstream test it came from,
with the titles replaced by the invented cast. They are the evidence that the
port is a port: a row that fails here is a place the port reads a name
differently from the server.

Pure string work: no files, no server, a few milliseconds a row.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest
from jfkit.naming import (
    EPISODE_EXPRESSIONS,
    EXTRA_RULES,
    MULTIPLE_EPISODE_EXPRESSIONS,
    UPSTREAM_COMMIT,
    episode_path,
    extra_type,
    parse,
    season_folder,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _table(name: str) -> list[dict[str, str]]:
    with (FIXTURES / name).open("r", encoding="utf-8", newline="") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


EPISODES = _table("naming_episodes.tsv")
SEASONS = _table("naming_seasons.tsv")
EXTRAS = _table("naming_extras.tsv")


def _expected(cell: str) -> int | None:
    return int(cell) if cell else None


def _windows(path: str) -> str:
    # upstream's second block is the first one again behind a drive letter,
    # written with backslashes; assembled here rather than written in the table
    return "C:" + path.replace("/", "\\")


# ------------------------------------------------------------------ episodes
@pytest.mark.parametrize("row", EPISODES, ids=lambda row: row["source"])
def test_every_upstream_episode_case_reads_as_upstream_asserts(row: dict[str, str]) -> None:
    flags = set(filter(None, row["flags"].split(",")))
    path = _windows(row["path"]) if "windows" in flags else row["path"]

    if row["call"] == "parser":
        found = episode_path(
            path,
            is_directory="directory" in flags,
            is_named=True if "named" in flags else None,
            is_optimistic=True if "optimistic" in flags else None,
            supports_absolute=True if "absolute" in flags else None,
        )
        if row["episode"] == "unresolved":
            assert not found.success
            return
        assert found.success
        season, episode, end = found.season, found.episode, found.end
        series, date = found.series_name, found.date
    else:
        verdict = parse(path, absolute_order="absolute" in flags)
        if row["episode"] == "unresolved":
            assert verdict.episode is None and not verdict.stub
            assert any("not a video file" in note for note in verdict.notes)
            return
        if row["episode"] == "stub":
            assert verdict.stub
            return
        season, episode, end = verdict.season, verdict.episode, verdict.end
        series, date = verdict.series_name, verdict.date

    if row["season"] != "*":
        assert season == _expected(row["season"])
    if row["episode"] != "*":
        assert episode == _expected(row["episode"])
    if row["end"] != "*":
        assert end == _expected(row["end"])
    if row["series"] != "*":
        # upstream compares the series name ignoring case
        assert (series or "").casefold() == row["series"].casefold()
    if row["date"] != "*":
        expected = dt.date.fromisoformat(row["date"]) if row["date"] else None
        assert date == expected


def test_the_episode_table_reaches_most_of_the_expressions() -> None:
    """Upstream's cases exercise the expressions; this says which of them.

    Twenty-two of the twenty-six claim at least one row. Of the other four,
    the two "series and season only" expressions have no episode number, so
    they can never claim a file -- they only lend a series name -- and the
    day-month-year date and the ``part 2`` expressions have no upstream test;
    ``test_naming_behaviours.py`` pins both.
    """
    claimed: set[int] = set()
    for row in EPISODES:
        flags = set(filter(None, row["flags"].split(",")))
        path = _windows(row["path"]) if "windows" in flags else row["path"]
        found = episode_path(path, is_directory="directory" in flags,
                             supports_absolute=True if "absolute" in flags else None)
        if found.expression is not None:
            claimed.add(found.expression)
    assert set(range(1, 27)) - claimed == {5, 12, 24, 25}


def test_the_tables_are_big_enough_to_be_a_specification() -> None:
    assert len(EPISODES) >= 200
    assert len(SEASONS) >= 75
    assert len(EXTRAS) >= 100


def test_the_tables_cite_the_commit_the_port_was_read_at() -> None:
    for name in ("naming_episodes.tsv", "naming_seasons.tsv", "naming_extras.tsv"):
        assert UPSTREAM_COMMIT[:10] in (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------ season folders
@pytest.mark.parametrize("row", SEASONS, ids=lambda row: row["source"])
def test_every_upstream_season_folder_case(row: dict[str, str]) -> None:
    tv = row["mode"] == "tv"
    found = season_folder(row["path"], row["parent"], special_aliases=tv, numeric_folders=tv)
    assert found.season == _expected(row["season"])
    assert found.is_season_folder is (row["folder"] == "yes")
    assert found.success is (found.season is not None)


# -------------------------------------------------------------------- extras
@pytest.mark.parametrize("row", EXTRAS, ids=lambda row: f"{row['source']}:{row['path']}")
def test_every_upstream_extras_case(row: dict[str, str]) -> None:
    rule = extra_type(row["path"], library_root=row["root"])
    assert (rule.extra_type if rule else "") == (row["extra"] or "")


# --------------------------------------------------------- the port's shape
def test_every_upstream_expression_is_ported_in_order() -> None:
    assert [e.number for e in EPISODE_EXPRESSIONS] == list(range(1, 27))
    assert len(MULTIPLE_EPISODE_EXPRESSIONS) == 10
    assert len(EXTRA_RULES) == 35
    lines = [e.line for e in EPISODE_EXPRESSIONS]
    assert lines == sorted(lines), "cited upstream lines follow the upstream order"
    assert [e.number for e in EPISODE_EXPRESSIONS if e.by_date] == [4, 5]
    assert [e.number for e in EPISODE_EXPRESSIONS if not e.supports_absolute] == [11]
    assert {e.number for e in EPISODE_EXPRESSIONS if e.optimistic} == {11, 18, 20, 21, 22, 23}
    assert {e.number for e in EPISODE_EXPRESSIONS if not e.named} == {2, 3, 4, 5, 8, 12, 19}
