"""The filename predictor, against a table of invented names.

The table is the specification and this file is the harness. Both exist
because the module reimplements a parser that belongs to somebody else: the
code cannot tell you when upstream changes, only a pinned set of examples can,
and the failure mode without them is a rename into the wrong episode rather
than an exception.

Every name here is invented. Nothing in this file, or in the table, describes
any real library.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from jfkit.naming import PARSED_AGAINST, Parse, Rule, clean_name, is_clean, parse

TABLE = Path(__file__).parent / "fixtures" / "filenames.tsv"


def _rows() -> list[dict[str, str]]:
    with TABLE.open("r", encoding="utf-8", newline="") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


ROWS = _rows()


def _number(value: str) -> int | None:
    return int(value) if value else None


def test_the_table_is_big_enough_to_be_a_specification() -> None:
    """A handful of examples pins nothing. Sixty pins the shapes."""
    assert len(ROWS) >= 60
    assert {row["rule"] for row in ROWS} >= {
        Rule.SEASON_EPISODE.value, Rule.ABSOLUTE.value,
        Rule.BY_DATE.value, Rule.CROSS.value, Rule.PAIR.value, Rule.EPISODE.value,
        Rule.NONE.value,
    }


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["path"])
def test_every_row_parses_as_the_table_says(row: dict[str, str]) -> None:
    result = parse(row["path"])
    assert result.season == _number(row["season"])
    assert result.episode == _number(row["episode"])
    assert result.end == _number(row["end"])
    assert result.rule.value == row["rule"]
    assert result.multi is (row["multi"] == "yes")


def test_the_version_it_was_checked_against_is_recorded() -> None:
    """Without this the answers are unattributable, which makes them useless."""
    assert "12.1" in PARSED_AGAINST


# --------------------------------------------------------------- the behaviours
# Each of these states one rule in its own right. The table proves they hold on
# sixty names; these say what the rule IS, so a failure names the behaviour
# rather than a row number.

def test_a_season_episode_marker_suppresses_the_absolute_expression() -> None:
    """This is why adding the marker is the durable cure for a range."""
    ranged = parse("/srv/media/series/Hollowmere/Hollowmere 2-14 The Ferry.avi")
    assert (ranged.episode, ranged.end) == (2, 14)
    fixed = parse("/srv/media/series/Hollowmere/Hollowmere - S02E14 - The Ferry.avi")
    assert (fixed.season, fixed.episode, fixed.end) == (2, 14, None)
    assert not fixed.would_get_end


def test_the_marker_is_read_only_from_the_last_path_segment() -> None:
    """A season folder does not protect the files inside it."""
    protected = parse("/srv/media/series/Nightjar/Season S01E09/Nightjar 1-05 A.avi")
    assert protected.end == 5


def test_an_end_number_can_be_attached_to_a_name_that_looked_clean() -> None:
    """The multi-episode expressions run after whatever claimed the name."""
    result = parse("/srv/media/series/Northwind/Northwind.S01E03E04.mkv")
    assert (result.season, result.episode, result.end) == (1, 3, 4)
    assert result.multi


def test_a_space_between_the_two_halves_of_the_marker_defeats_the_range() -> None:
    """The multi expressions allow a dot or an x between S and E -- not a space.

    The consequence is worth knowing rather than fixing: the second episode of
    such a two-parter is simply not represented, and no API write can express
    it.
    """
    result = parse("/srv/media/series/Northwind/Northwind S01 E03-04 - Two Parts.mkv")
    assert (result.season, result.episode, result.end) == (1, 3, None)


def test_a_bare_number_is_an_absolute_episode_number() -> None:
    assert parse("/srv/media/series/Coldwater/Coldwater 05.mkv").episode == 5
    assert parse("/srv/media/series/Coldwater/Coldwater 100 The Long One.avi").episode == 100


def test_a_year_in_a_title_is_read_as_an_episode_number() -> None:
    """Not a bug in this module: it is what the expression does."""
    assert parse("/srv/media/series/Coldwater/Coldwater 1985 The Year.avi").episode == 1985


def test_separators_that_look_the_same_are_not() -> None:
    """Dots and underscores around the number defeat the absolute expression.

    They do not make the name unclaimed: the unanchored ``1-12`` expression
    further down the list reads the pair as season 1, episode 5 -- a different
    answer to the same digits, and no range.
    """
    for name in ("Coldwater.1-05.Pilot.avi", "Coldwater_1-05 Pilot.avi"):
        found = parse(f"/srv/media/series/Coldwater/{name}")
        assert (found.rule, found.season, found.episode, found.end) == (Rule.PAIR, 1, 5, None)
    assert parse("/srv/media/series/Coldwater/Coldwater 1-05 Pilot.avi").end == 5


def test_an_x_after_the_number_stops_the_absolute_expression() -> None:
    """One letter of difference, two different parses, in the same directory.

    The expression's tail excludes an ``x``, and upstream compiles every
    expression case-insensitively, so any ordinary word containing one -- a
    codec token, or the word "extended", in either case -- changes the answer
    from a range to the season-and-episode pair. This is the most surprising
    behaviour in the set, and the reason a table of names exists at all.
    """
    for word in ("extended", "EXTENDED"):
        found = parse(f"/srv/media/series/Coldwater/Coldwater 1-05 {word}.avi")
        assert (found.rule, found.season, found.episode, found.end) == (Rule.PAIR, 1, 5, None)
    assert parse("/srv/media/series/Coldwater/Coldwater 1-05 plain.avi").end == 5


def test_a_name_beginning_with_the_word_episode_is_left_to_its_own_expression() -> None:
    """The absolute expression refuses it; the ``Episode N`` expression takes it."""
    found = parse("/srv/media/series/Coldwater/Episode 1-05 Pilot.avi")
    assert (found.rule, found.episode, found.end) == (Rule.EPISODE, 1, 5)
    assert found.expression == 13
    assert parse("/srv/media/series/Coldwater/Folge 04.mkv").episode == 4


def test_a_date_in_the_name_is_reported_as_such() -> None:
    result = parse("/srv/media/series/Signal Hill/Signal Hill (2009-04-02).mp4")
    assert result.rule is Rule.BY_DATE
    assert result.episode is None
    assert any("refresh" in note for note in result.notes)


def test_a_marker_beats_a_date_in_the_same_name() -> None:
    result = parse("/srv/media/series/Signal Hill/Signal Hill - S01E02 - 2009-04-02.mkv")
    assert (result.rule, result.season, result.episode) == (Rule.SEASON_EPISODE, 1, 2)


def test_both_separators_and_a_bare_name_are_accepted() -> None:
    """The original tool required a separator and rewrote every path first.

    That made the answer depend on the platform and on the working directory,
    which is not a property a predictor may have.
    """
    windows = parse("C:" + "\\" + "Media" + "\\" + "Northwind - S01E03.mkv")
    posix = parse("/srv/media/series/Northwind - S01E03.mkv")
    bare = parse("Northwind - S01E03.mkv")
    assert windows.episode == posix.episode == bare.episode == 3


def test_a_season_folder_is_reported_in_the_notes_and_never_in_the_numbers() -> None:
    result = parse("/srv/media/series/Nightjar/Season 04/05 - The Quiet One.mkv")
    assert result.season is None
    assert any("season 4" in note for note in result.notes)


def test_the_range_note_says_what_it_costs() -> None:
    result = parse("/srv/media/series/Nightjar/Nightjar 1-05 Pilot.avi")
    assert any("only a rename" in note for note in result.notes)


def test_is_clean_is_the_check_to_run_before_and_after_a_rename() -> None:
    assert is_clean("/srv/media/series/Northwind - S01E03 - A.mkv", 1, 3)
    assert not is_clean("/srv/media/series/Northwind - S01E03 - A.mkv", 1, 4)
    assert not is_clean("/srv/media/series/Northwind.S01E03E04.mkv", 1, 3)


def test_clean_name_round_trips_through_the_parser() -> None:
    for season, episode in ((1, 3), (0, 1), (11, 24), (2, 0)):
        name = clean_name("Northwind", season, episode, "A Title")
        assert is_clean(f"/srv/media/series/Northwind/{name}", season, episode)


def test_clean_name_refuses_a_negative_number() -> None:
    with pytest.raises(ValueError, match="not negative"):
        clean_name("Northwind", -1, 3)


def test_a_parse_describes_itself_without_leaking_the_path() -> None:
    result: Parse = parse("/srv/media/series/Northwind/Northwind.S01E03E04.mkv")
    described = result.describe()
    assert "S=1" in described and "E=3" in described and "end=4" in described
    assert "/srv/" not in described
