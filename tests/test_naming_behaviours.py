"""The naming port, one behaviour at a time.

``test_naming_upstream.py`` proves the port reads upstream's own cases the way
upstream does. This file states, by name, the behaviours that decide a rename:
each one is a way a name that looks harmless is read as something else. Every
name is invented.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pytest
from jfkit.naming import (
    EXTRA_FOLDER_NAMES,
    MAX_PATH,
    PARSED_AGAINST,
    UPSTREAM_COMMIT,
    VIDEO_SUFFIXES,
    Rule,
    episode_path,
    extra_type,
    longest_derived_path,
    near_miss_extra_folder,
    parse,
    season_folder,
)

SERIES = "/srv/media/series"


# ------------------------------------------------------ finding 4: foo.E01.
@pytest.mark.parametrize(("name", "episode"), [
    ("E16.The Quiet One (1).mkv", 16),
    ("E17.The Quiet One (2).mkv", 17),
    ("E18.Winter Tide.mkv", 18),
    ("Nightjar.e05.The Anchor.mkv", 5),
])
def test_an_episode_marker_followed_by_a_dot_is_an_episode(name: str, episode: int) -> None:
    """The Kodi ``foo.E01.`` expression: an episode number, no season."""
    found = parse(f"{SERIES}/Nightjar/Season 2/{name}")
    assert (found.rule, found.episode, found.season) == (Rule.EPISODE, episode, None)
    assert found.expression == 3
    assert found.season_from_folder == 2
    assert found.effective_season == 2


def test_an_ep_token_is_an_episode() -> None:
    found = parse(f"{SERIES}/Nightjar/Nightjar.ep_07.mkv")
    assert (found.rule, found.episode, found.expression) == (Rule.EPISODE, 7, 2)


# ------------------------------------------ finding 5: the optimistic digits
def test_a_digit_run_after_a_separator_is_a_season_and_an_episode() -> None:
    """``8000`` is season 80, episode 00 -- the last two digits are the episode."""
    found = parse("/srv/media/documentaries/Nightjar-8000 Quiet Anchors (1_2).mp4")
    assert (found.rule, found.season, found.episode) == (Rule.OPTIMISTIC, 80, 0)
    assert found.optimistic


def test_a_dotted_thousand_is_a_special() -> None:
    """The "obvious" fix ``8.000`` reads as season 0, episode 0: a special."""
    found = parse("/srv/media/documentaries/Nightjar-8.000 Quiet Anchors (1_2).mp4")
    assert (found.rule, found.season, found.episode) == (Rule.OPTIMISTIC, 0, 0)


def test_the_season_guard_keeps_long_numeric_ids_from_parsing() -> None:
    """Seasons 200-1927 and above 2500 are rejected, and so is a number too
    long for a 32-bit integer, which is what keeps a broadcaster's long
    numeric ids from parsing as seasons."""
    assert parse("/srv/media/documentaries/Nightjar-0352352995.mp4").rule is Rule.NONE
    assert not episode_path("Nightjar Special (1920x1080).mkv").success
    # 1928 is inside the allowed window, 2500 too, 2501 is not
    assert episode_path("/x/Nightjar S1928E01.mkv").season == 1928
    assert episode_path("/x/Nightjar S2500E01.mkv").season == 2500
    assert episode_path("/x/Nightjar S2501E01.mkv").season != 2501


def test_absolute_order_leaves_the_optimistic_expression_out() -> None:
    name = "/srv/media/series/Nightjar/Nightjar_102.mkv"
    assert (parse(name).season, parse(name).episode) == (1, 2)
    absolute = parse(name, absolute_order=True)
    assert absolute.rule is not Rule.OPTIMISTIC


# ------------------------------------------------ finding 6: extras folders
@pytest.mark.parametrize("folder", sorted(EXTRA_FOLDER_NAMES - {"theme-music"}))
def test_a_file_directly_in_an_extras_folder_is_an_extra(folder: str) -> None:
    found = parse(f"{SERIES}/Nightjar/S07/{folder.title()}/The Truth About Season 7.mkv")
    assert found.rule is Rule.EXTRA and found.is_extra
    assert (found.season, found.episode, found.end) == (None, None, None)
    assert "not an episode" in found.describe()


def test_the_featurette_folder_names_its_type() -> None:
    found = parse(f"{SERIES}/Nightjar/S07/Featurettes/The Truth About Season 7.mkv")
    assert found.extra == "Featurette"


def test_a_misspelt_extras_folder_is_an_ordinary_folder_and_says_so() -> None:
    """The bug class that put a featurette on an episode slot."""
    found = parse(f"{SERIES}/Nightjar/S07/Feaaturettes/The Truth About Season 7.mkv")
    assert not found.is_extra
    assert (found.rule, found.episode, found.effective_season) == (Rule.ABSOLUTE, 7, 7)
    assert found.warnings and "featurettes" in found.warnings[0]


@pytest.mark.parametrize(("name", "meant"), [
    ("Featurete", "featurettes"), ("Behind The Scene", "behind the scenes"),
    ("Intervews", "interviews"), ("Trailer", "trailers"), ("Extrass", "extras"),
])
def test_a_near_miss_names_the_folder_it_was_meant_to_be(name: str, meant: str) -> None:
    assert near_miss_extra_folder(name) == meant


@pytest.mark.parametrize("name", [
    "Season 01", "Specials", "S07", "Northwind", "Featurettes", "extras", "Scene 1 and 2",
])
def test_ordinary_and_exact_folder_names_are_not_near_misses(name: str) -> None:
    assert near_miss_extra_folder(name) is None


def test_only_the_folder_a_file_sits_in_makes_it_an_extra() -> None:
    found = parse(f"{SERIES}/Nightjar/Featurettes/Disc 1/Nightjar - S01E01.mkv")
    assert not found.is_extra and found.episode == 1
    assert any("further up" in note for note in found.notes)


def test_a_suffix_makes_a_file_an_extra_but_a_word_in_the_title_does_not() -> None:
    assert parse(f"{SERIES}/Nightjar/Nightjar S01E01-trailer2.mkv").extra == "Trailer"
    assert parse(f"{SERIES}/Nightjar/Nightjar S01E01 The Golden Sample.mkv").extra is None
    assert extra_type("/x/Blue Canyon-featurette.mkv") is not None


# ------------------------------------------------ dates, parts, stubs, files
def test_a_day_month_year_date_is_a_by_date_episode() -> None:
    found = parse(f"{SERIES}/Signal Hill/Signal Hill 02.04.2009 Late.mkv")
    assert (found.rule, found.expression) == (Rule.BY_DATE, 5)
    assert found.date == dt.date(2009, 4, 2)
    assert found.episode is None


def test_an_impossible_date_is_still_by_date_and_carries_no_date() -> None:
    """Upstream marks the match a success whether or not the date parses."""
    found = parse(f"{SERIES}/Signal Hill/Signal Hill 2009.13.40 Late.mkv")
    assert found.rule is Rule.BY_DATE and found.date is None


def test_a_part_number_is_an_episode_number() -> None:
    found = parse(f"{SERIES}/Coldwater/Coldwater.part.2.mkv")
    assert (found.rule, found.episode, found.expression) == (Rule.PART, 2, 12)
    # a roman part number matches the expression but is not a number
    assert parse(f"{SERIES}/Coldwater/Coldwater.part.ii.mkv").expression != 12


def test_a_file_the_server_does_not_resolve_is_not_an_episode() -> None:
    found = parse(f"{SERIES}/Northwind/Northwind - S01E03.nfo")
    assert found.rule is Rule.NONE and found.episode is None
    assert any("not a video file" in note for note in found.notes)
    assert ".strm" in VIDEO_SUFFIXES and ".iso" in VIDEO_SUFFIXES


def test_a_disc_placeholder_is_a_stub() -> None:
    found = parse(f"{SERIES}/Northwind/Northwind - S01E03.dvd.disc")
    assert found.stub and found.episode == 3


# ----------------------------------------------------- season folders
def test_a_season_folder_supplies_the_season_the_name_leaves_out() -> None:
    found = parse(f"{SERIES}/Nightjar/Season 04/05 - The Quiet One.mkv")
    assert (found.season, found.episode) == (None, 5)
    assert (found.season_from_folder, found.effective_season) == (4, 4)


def test_a_folder_without_a_keyword_supplies_no_season() -> None:
    found = parse(f"{SERIES}/Nightjar/01 - The First Year/05 - The Quiet One.mkv")
    assert found.season_from_folder is None
    assert not season_folder("01 - The First Year", "Nightjar").is_season_folder
    assert season_folder("Specials", "Nightjar").season == 0
    assert season_folder("Nightjar Season 2", "Nightjar").season == 2


def test_a_file_in_a_plain_folder_inside_a_season_takes_the_folders_numbers() -> None:
    """LibraryManager reads the folder when the file claims nothing."""
    found = parse(f"{SERIES}/Nightjar/Season 1/Nightjar S01E04/video.mkv")
    assert (found.season, found.episode) == (1, 4)
    assert any("from its folder" in note for note in found.notes)


# ----------------------------------------------------- finding 22: lengths
def test_the_longest_derived_path_is_the_preview_tiles() -> None:
    path = "/srv/media/series/Northwind/Season 01/Northwind - S01E03.mkv"
    assert longest_derived_path(path) == len(path) - len(".mkv") + len(
        ".trickplay/320 - 10x10/000.jpg"
    )
    assert MAX_PATH == 259


# ------------------------------------------------------------ the record
def test_the_version_and_commit_are_recorded() -> None:
    assert "12.1" in PARSED_AGAINST and UPSTREAM_COMMIT[:10] in PARSED_AGAINST


def test_a_deep_path_is_still_parsed_quickly() -> None:
    """Two upstream expressions backtrack badly on deep paths in Python's
    engine; the port's rewrites keep a forty-folder path under a second."""
    deep = "/" + "/".join(f"folder number {i}" for i in range(40)) + "/nothing here.mkv"
    started = time.perf_counter()
    parse(deep)
    assert time.perf_counter() - started < 1.0


# ------------------------------------------------------------ the verb
def test_the_verb_marks_extras_and_near_misses_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jfkit import cli

    config = tmp_path / "mediatoolkit.toml"
    config.write_text("", encoding="utf-8")
    extra = f"{SERIES}/Nightjar/S07/Featurettes/The Truth About Season 7.mkv"
    near = f"{SERIES}/Nightjar/S07/Feaaturettes/The Truth About Season 7.mkv"
    clean = f"{SERIES}/Nightjar/S07/Nightjar - S07E01.mkv"
    status = cli.main(["--config", str(config), "naming", extra, near, clean,
                       "--only-problems", "--full-paths"])
    printed = capsys.readouterr().out
    assert status == 0
    assert f"EXTRA extra (featurette), not an episode [extra]  {extra}" in printed
    assert f"WARN  S=- E=7 end=- [absolute]  {near}" in printed
    assert "warning: the folder 'Feaaturettes'" in printed
    assert clean not in printed
    assert "1 extra(s), 1 with a warning" in printed


def test_the_verb_says_what_an_extra_does_to_the_exit_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jfkit import cli

    with pytest.raises(SystemExit):
        cli.main(["naming", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "(EXTRA)" in text and "(WARN)" in text and "--absolute-order" in text
