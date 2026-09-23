"""The six surveys, over records this file writes by hand.

Records rather than a server, because the question each survey answers is
about the records and not about the transport -- and because a hand-written
record is the only way to state the awkward case precisely: the item whose
name is its own filename, the track with no bitrate, the marks that are
exactly ten minutes apart.

One test does go through the stand-in server, and it is the one that matters
for correctness of the whole package: the fetch is user-scoped, because the
unscoped route answers short and says nothing about it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from jfkit.dto import TICKS_PER_SECOND
from jfkit.surveys import BUILDERS, build, fetch_items, names
from jfkit.surveys.chapters import FIXED_INTERVAL_CV, classify, gap_variation
from jfkit.surveys.completeness import title_class
from jfkit.surveys.languages import estimated_bytes

from tests.fake_server import ITEMS, USER_ID, Recorder, client_for, fake_server

MINUTE = 60 * TICKS_PER_SECOND


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def movie(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "Id": "00000000-0000-0000-0000-000000000001",
        "Type": "Movie",
        "Name": "The Quiet Harbour",
        "Path": "/srv/media/movies/The Quiet Harbour (1978)/the-quiet-harbour.mkv",
        "ProviderIds": {"Tmdb": "1001"},
        "Overview": "An invented description.",
        "PremiereDate": "1978-04-03T22:00:00Z",
        "Genres": ["Drama"],
        "People": [{"Name": "Someone"}],
        "Studios": [{"Name": "A studio"}],
        "ImageTags": {"Primary": "abc"},
        "BackdropImageTags": ["def"],
        "CommunityRating": 7.1,
        "RunTimeTicks": 90 * MINUTE,
        "MediaStreams": [],
        "MediaSources": [{"Container": "mkv", "Size": 8 * 2**30}],
        "Chapters": [],
    }
    base.update(over)
    return base


def episode(**over: Any) -> dict[str, Any]:
    base = movie(
        Id="00000000-0000-0000-0000-000000000002",
        Type="Episode",
        Name="Harbour Lights",
        Path="/srv/media/series/Harbour Lights/Season 01/"
             "Harbour.Lights.S01E02.mkv",
        IndexNumber=2,
        ParentIndexNumber=1,
        RunTimeTicks=45 * MINUTE,
    )
    base.update(over)
    return base


def audio(index: int, language: str, **over: Any) -> dict[str, Any]:
    stream: dict[str, Any] = {
        "Type": "Audio", "Index": index, "Codec": "ac3", "Language": language,
        "Channels": 6, "IsDefault": index == 1, "BitRate": 640_000,
    }
    stream.update(over)
    return stream


# ------------------------------------------------------------------ registry
def test_every_survey_is_registered_and_builds_from_nothing() -> None:
    assert names() == [
        "audio-languages", "chapters", "containers", "duplicates",
        "filename-parse", "metadata",
    ]
    for name in names():
        survey = build(name, [])
        assert survey.caveats, f"{name} has no caveats"
        assert survey.about


def test_an_unknown_survey_is_refused() -> None:
    with pytest.raises(ValueError, match="not one of"):
        build("everything", [])


def test_the_fetch_is_user_scoped(server: tuple[str, Recorder]) -> None:
    """The one that keeps every survey in this package honest.

    The unscoped route answers with fewer items than exist and gives no sign
    of it, so a survey built on it is quietly short.
    """
    url, recorder = server
    rows = fetch_items(client_for(url))
    assert len(rows) == len(ITEMS)
    assert all(
        route.startswith(f"/Users/{USER_ID}")
        for method, route in recorder.requests if method == "GET"
    )


# -------------------------------------------------------------- completeness
def test_a_name_that_is_the_filename_is_not_a_title() -> None:
    name = "Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv"
    path = f"/srv/media/series/Northwind/{name}"
    assert title_class(name.removesuffix(".mkv"), path) == "untitled"
    assert title_class(name) == "untitled"
    assert title_class("Episode 4") == "untitled"
    assert title_class("The Quiet Harbour") == "titled"
    assert title_class(None) == "untitled"


def test_a_title_behind_a_numbering_prefix_is_its_own_answer() -> None:
    """Counting these as missing overstates the work by a long way."""
    assert title_class("Northwind - S01E03 - The Quiet Harbour") == "prefixed"
    assert title_class("Northwind - S01E03 - x264") == "untitled"


def test_a_name_equal_to_its_own_basename_is_not_a_title() -> None:
    assert title_class(
        "the-quiet-harbour", "/srv/media/movies/the-quiet-harbour.mkv"
    ) == "untitled"


def test_completeness_counts_what_each_type_is_scored_on() -> None:
    survey = build("metadata", [movie(), movie(Overview=None, Genres=[])])
    assert len(survey.rows) == 2
    assert survey.rows[0]["complete"] is True
    assert survey.rows[1]["missing"] == "overview,genres"
    assert survey.summary["complete"] == 1


def test_a_type_nobody_scores_is_skipped_rather_than_scored_as_empty() -> None:
    survey = build("metadata", [movie(Type="Audio")])
    assert survey.rows == []


# ----------------------------------------------------------------- languages
def test_an_estimate_with_a_missing_number_is_not_zero() -> None:
    assert estimated_bytes({"BitRate": 640_000}, None) is None
    assert estimated_bytes({}, 90 * MINUTE) is None
    assert estimated_bytes({"BitRate": 640_000}, 90 * MINUTE) == 432_000_000


def test_one_row_per_track_and_the_unlabelled_ones_are_counted() -> None:
    survey = build("audio-languages", [
        movie(MediaStreams=[audio(1, "eng"), audio(2, "deu"), audio(3, "und")]),
    ])
    assert len(survey.rows) == 3
    assert [row["determined"] for row in survey.rows] == [True, True, False]
    assert survey.summary["tracks with no language claimed"] == 1


def test_a_track_with_no_bitrate_is_counted_rather_than_estimated_at_nothing() -> None:
    survey = build("audio-languages", [
        movie(MediaStreams=[audio(1, "eng", BitRate=None)]),
    ])
    assert survey.rows[0]["estimated_mib"] is None
    assert survey.summary["tracks whose size could not be estimated"] == 1


def test_an_item_with_no_default_track_is_reported() -> None:
    survey = build("audio-languages", [
        movie(MediaStreams=[audio(1, "eng", IsDefault=False)]),
        movie(MediaStreams=[audio(1, "eng"), audio(2, "deu", IsDefault=True)]),
    ])
    assert survey.summary["items with no default track"] == 1
    assert survey.summary["items with more than one default track"] == 1


# ------------------------------------------------------------------ chapters
def marks(count: int, step_s: float, named: bool = False) -> list[dict[str, Any]]:
    return [
        {"StartPositionTicks": int(i * step_s * TICKS_PER_SECOND),
         "Name": f"A real name {i}" if named else f"Chapter {i + 1}"}
        for i in range(count)
    ]


def test_marks_with_generated_names_are_marks_with_no_names() -> None:
    assert classify(marks(8, 300))[0] == "generated"
    assert classify(marks(8, 300, named=True))[0] == "named"
    assert classify([])[0] == "none"


def test_a_file_where_some_marks_are_named_is_its_own_answer() -> None:
    mixed = marks(4, 300)
    mixed[1]["Name"] = "The harbour at night"
    assert classify(mixed) == ("mixed", 4, 1)


def test_evenly_spaced_marks_are_recognisable_by_their_spacing() -> None:
    """The signature of a tool rather than a person: the gaps do not vary."""
    assert gap_variation([i * 600 * TICKS_PER_SECOND for i in range(10)]) == 0.0
    uneven = [0, 412, 1190, 1755, 2980, 3111, 4502]
    variation = gap_variation([t * TICKS_PER_SECOND for t in uneven])
    assert variation is not None and variation > FIXED_INTERVAL_CV


def test_too_few_marks_to_judge_says_so_rather_than_guessing() -> None:
    assert gap_variation([0, 600 * TICKS_PER_SECOND]) is None
    survey = build("chapters", [movie(Chapters=marks(2, 600))])
    assert survey.rows[0]["gap_variation"] is None
    assert survey.summary["too few marks to judge the spacing"] == 1


def test_the_chapter_survey_reports_the_state_and_the_spacing() -> None:
    """The two questions are separate: whether it is named, and who put it there.

    A named set is usually unevenly spaced and a generated one usually is not,
    but neither implies the other, so both columns are reported.
    """
    hand_made = [
        {"StartPositionTicks": int(t * TICKS_PER_SECOND), "Name": f"A real name {i}"}
        for i, t in enumerate([0, 412, 1190, 1755, 2980, 3111])
    ]
    survey = build("chapters", [
        movie(Chapters=marks(10, 600)),
        movie(Chapters=[]),
        episode(Chapters=hand_made),
    ])
    assert [row["state"] for row in survey.rows] == ["generated", "none", "named"]
    assert survey.rows[0]["fixed_interval"] is True
    assert survey.rows[2]["fixed_interval"] is False
    assert survey.summary["evenly spaced, so generated by a tool"] == 1


# ----------------------------------------------------------------- inventory
def test_the_container_census_counts_what_is_in_use() -> None:
    survey = build("containers", [
        movie(MediaStreams=[
            {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080},
            audio(1, "eng"),
        ]),
        movie(MediaSources=[{"Container": "mp4", "Size": 2 * 2**30}]),
    ])
    assert survey.rows[0]["resolution"] == "1920x1080"
    assert survey.rows[0]["audio_tracks"] == 1
    assert survey.summary["mkv / h264"] == 1
    assert survey.summary["mp4 / (unknown)"] == 1


def test_an_item_with_no_size_is_counted_not_added_as_zero() -> None:
    survey = build("containers", [movie(MediaSources=[{"Container": "mkv"}])])
    assert survey.rows[0]["size_gib"] is None
    assert survey.summary["items with no size recorded"] == 1


def test_duplicates_group_by_identity_and_not_by_name() -> None:
    survey = build("duplicates", [
        movie(Id="00000000-0000-0000-0000-000000000001", Name="The Quiet Harbour"),
        movie(Id="00000000-0000-0000-0000-000000000002",
              Name="Quiet Harbour, The (restored)",
              MediaSources=[{"Container": "mkv", "Size": 20 * 2**30}]),
        movie(Id="00000000-0000-0000-0000-000000000003", Name="Blue Canyon",
              ProviderIds={"Tmdb": "2002"}),
    ])
    assert survey.summary["groups"] == 1
    assert len(survey.rows) == 2
    assert survey.summary["at most reclaimable"] == "8.0 GiB"


def test_an_item_with_no_identifier_is_not_grouped_with_anything() -> None:
    survey = build("duplicates", [movie(ProviderIds={}), movie(ProviderIds={})])
    assert survey.rows == []


def test_the_filename_survey_predicts_a_range_before_the_rename() -> None:
    """The outcome that cannot be fixed afterwards, reported beforehand."""
    survey = build("filename-parse", [
        episode(Path="/srv/media/series/Northwind/Northwind.S01E03E04.mkv"),
        episode(Path="/srv/media/series/Northwind/Northwind.S01E03.mkv"),
    ])
    assert survey.rows[0]["would_get_range"] is True
    assert survey.rows[1]["would_get_range"] is False
    assert survey.summary["would be read as a range"] == 1


def test_a_path_that_disagrees_with_the_catalogue_is_flagged() -> None:
    survey = build("filename-parse", [
        episode(Path="/srv/media/series/Northwind/Northwind.S01E09.mkv",
                IndexNumber=2, ParentIndexNumber=1),
    ])
    assert survey.rows[0]["matches_catalogue"] is False
    assert survey.summary["disagreeing with the catalogue"] == 1


def test_an_item_with_no_path_is_skipped() -> None:
    survey = build("filename-parse", [episode(Path=None)])
    assert survey.rows == []


# --------------------------------------------------------------- the shape
def test_every_survey_renders_in_every_format() -> None:
    from jfkit.report import FORMATS, render

    items = [movie(MediaStreams=[audio(1, "eng")], Chapters=marks(6, 480)), episode()]
    for name in BUILDERS:
        survey = build(name, items)
        for fmt in FORMATS:
            assert render(survey, fmt)  # type: ignore[arg-type]
