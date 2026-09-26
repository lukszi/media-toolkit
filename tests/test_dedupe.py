"""Duplicate resolution: grouping, the owner's rules, the keeper check, the plan.

The rules are tested on copies described in memory, because each rule is a
statement about tracks and a file is only a way of carrying them; the tests
that read real files are in ``test_dedupe_media.py``. The plan is applied
against the stand-in server, on a few bytes of text per "film", with the
probe and the payload check replaced by name -- those tests are about the
order of the steps, the watched state and what gets moved where.

The cases worth reading first: a keeper whose payload is not there blocks
its group (the loss this tool exists to prevent); two shows with one name
are never grouped; the segments of one episode are not copies; and a
conflict between the rules keeps both copies instead of forcing a pick.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jfkit import cli as jfkit_cli
from jfkit import userdata
from jfkit.dedupe import (
    BLOCKED,
    KEEP_BOTH,
    NOT_DUPLICATE,
    SAFE,
    SEGMENTS,
    Copy,
    Member,
    Rules,
    Track,
    actions,
    build_plan,
    choose,
    coverage,
    find_groups,
    members_of,
    parked_path,
    resolution_class,
    resolve,
    segment_of,
)
from jfkit.dedupe.facts import origin_of
from jfkit.safedelete import Candidate, safe_delete
from mkvkit.config import ConfigError, DedupePolicy, loads
from mkvkit.integrity import IntegrityReport
from mkvkit.steps import apply, read_audit

from tests.fake_server import (
    CREDENTIAL_VARIABLE,
    FIXTURE_CREDENTIAL,
    SECOND_USER_ID,
    THIRD_USER_ID,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)

RULES = Rules(keep_languages=("eng", "deu"))


def ident(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


# ------------------------------------------------------------------ builders
def member(path: str = "/srv/media/movies/The Quiet Harbour (1978)/a.mkv",
           item_id: str = ident(1), **over: Any) -> Member:
    base: dict[str, Any] = {"item_id": item_id, "name": "The Quiet Harbour",
                            "path": path, "kind": "Movie", "year": 1978}
    base.update(over)
    return Member(**base)


def audio(language: str, channels: int = 6, codec: str = "ac3", **over: Any) -> Track:
    return Track(kind="audio", language=language, codec=codec, channels=channels, **over)


def subtitle(language: str, **over: Any) -> Track:
    return Track(kind="subtitle", language=language, codec="subrip", **over)


def copy(name: str = "a", *, audio_tracks: tuple[Track, ...] = (audio("eng"),),
         subtitles: tuple[Track, ...] = (), duration: float | None = 5400.0,
         width: int = 1920, height: int = 1080, origin: str = "unknown",
         bitrate: int = 10_000_000, size: int = 10 * 2**30, n: int = 1) -> Copy:
    return Copy(
        member=member(f"/srv/media/movies/The Quiet Harbour (1978)/{name}.mkv", ident(n)),
        size=size, duration_s=duration, width=width, height=height, bitrate=bitrate,
        audio=audio_tracks, subtitles=subtitles, origin=origin,
    )


def movie_row(n: int, name: str, path: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Id": ident(n), "Name": name, "Type": "Movie", "Path": path,
        "ProductionYear": 1978, "ProviderIds": {"Tmdb": "1001"},
        "RunTimeTicks": 5400 * 10_000_000,
        "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False,
                     "IsFavorite": False},
    }
    row.update(over)
    return row


def episode_row(n: int, series_id: str, series: str, path: str, season: int = 1,
                episode: int = 1, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Id": ident(n), "Name": f"Episode {episode}", "Type": "Episode", "Path": path,
        "SeriesId": series_id, "SeriesName": series, "ParentIndexNumber": season,
        "IndexNumber": episode, "ProviderIds": {"Tvdb": str(5000 + 100 * season + episode)},
    }
    row.update(over)
    return row


# ================================================================== grouping
def test_films_group_by_identifier_and_never_by_name() -> None:
    scan = find_groups([
        movie_row(1, "The Quiet Harbour", "/srv/media/movies/a/one.mkv"),
        movie_row(2, "Quiet Harbour, The (restored)", "/srv/media/movies/b/two.mkv"),
        movie_row(3, "The Quiet Harbour", "/srv/media/movies/c/three.mkv",
                  ProviderIds={"Tmdb": "2002"}),
    ])
    assert len(scan.groups) == 1
    assert {m.item_id for m in scan.groups[0].members} == {ident(1), ident(2)}
    assert scan.groups[0].key == "Tmdb=1001"


def test_a_second_identifier_links_a_third_copy() -> None:
    scan = find_groups([
        movie_row(1, "Blue Canyon", "/srv/media/movies/a/a.mkv",
                  ProviderIds={"Tmdb": "7"}),
        movie_row(2, "Blue Canyon", "/srv/media/movies/b/b.mkv",
                  ProviderIds={"Tmdb": "7", "Imdb": "tt0000007"}),
        movie_row(3, "Blue Canyon", "/srv/media/movies/c/c.mkv",
                  ProviderIds={"Imdb": "tt0000007"}),
    ])
    assert [len(g.members) for g in scan.groups] == [3]


def test_rows_that_disagree_on_an_identifier_are_not_duplicates() -> None:
    scan = find_groups([
        movie_row(1, "Winter Tide", "/srv/media/movies/a/a.mkv",
                  ProviderIds={"Tmdb": "9", "Imdb": "tt0000001"}),
        movie_row(2, "Winter Tide", "/srv/media/movies/b/b.mkv",
                  ProviderIds={"Tmdb": "9", "Imdb": "tt0000002"}),
    ])
    (group,) = scan.groups
    assert group.not_duplicate is not None and "disagree" in group.not_duplicate


def test_a_film_without_an_identifier_is_counted_and_not_grouped() -> None:
    scan = find_groups([
        movie_row(1, "Hollow Lantern", "/srv/media/movies/a/a.mkv", ProviderIds={}),
        movie_row(2, "Hollow Lantern", "/srv/media/movies/b/b.mkv", ProviderIds={}),
    ])
    assert scan.groups == () and scan.no_identifier == 2


def test_two_shows_with_one_name_are_never_grouped() -> None:
    """The case that once produced a verdict about two unrelated episodes."""
    scan = find_groups([
        episode_row(10, ident(100), "Signal Hill", "/srv/media/series/Signal Hill/S01E01.mkv"),
        episode_row(11, ident(200), "Signal Hill",
                    "/srv/media/series/Signal Hill (2019)/S01E01.mkv"),
    ])
    assert scan.groups == ()


def test_episodes_group_by_series_season_and_episode() -> None:
    scan = find_groups([
        episode_row(10, ident(100), "Northwind", "/srv/media/series/Northwind/a/S01E03.mkv",
                    episode=3),
        episode_row(11, ident(100), "Northwind", "/srv/media/series/Northwind/b/S01E03.mkv",
                    episode=3, ProviderIds={"Tvdb": "5103", "Imdb": "tt0000103"}),
        episode_row(12, ident(100), "Northwind", "/srv/media/series/Northwind/S01E04.mkv",
                    episode=4),
    ])
    (group,) = scan.groups
    assert group.kind == "Episode" and len(group.members) == 2
    assert group.key.endswith("S01E03")


def test_a_slot_alone_does_not_make_copies() -> None:
    """A folder of several shows numbered in one sequence shares slots, not episodes."""
    folder = "/srv/media/series/Signal Hill and Northwind"
    scan = find_groups([
        episode_row(10, ident(100), "Signal Hill", f"{folder}/SH S01 E03 - Hollowmere.mkv",
                    episode=3, ProviderIds={}, Name="Hollowmere"),
        episode_row(11, ident(100), "Signal Hill", f"{folder}/NW S01 E03 - Amberlight.mkv",
                    episode=3, ProviderIds={}, Name="Hollowmere"),
    ])
    (group,) = scan.groups
    assert group.not_duplicate is not None and "share no provider" in group.not_duplicate


def test_episodes_that_disagree_on_an_identifier_are_not_copies() -> None:
    scan = find_groups([
        episode_row(10, ident(100), "Northwind", "/srv/media/series/Northwind/a/S01E03.mkv",
                    episode=3, ProviderIds={"Tvdb": "1"}),
        episode_row(11, ident(100), "Northwind", "/srv/media/series/Northwind/b/S01E03.mkv",
                    episode=3, ProviderIds={"Tvdb": "2"}),
    ])
    (group,) = scan.groups
    assert group.not_duplicate is not None and "disagree" in group.not_duplicate


def test_a_double_episode_is_not_a_copy_of_its_first_half() -> None:
    scan = find_groups([
        episode_row(10, ident(100), "Northwind", "/srv/media/series/Northwind.S01E03E04.mkv",
                    episode=3, IndexNumberEnd=4),
        episode_row(11, ident(100), "Northwind", "/srv/media/series/Northwind.S01E03.mkv",
                    episode=3),
    ])
    assert scan.groups == ()


@pytest.mark.parametrize(("name", "segment"), [
    ("Harbour Lights - S01E01a.mkv", "a"),
    ("Harbour.Lights.S01E01B.mkv", "b"),
    ("Harbour Lights - S01E01c - Pilot.mkv", "c"),
    ("Northwind.S01E03E04.mkv", None),
    ("Northwind.S01E03v2.mkv", None),
    ("Northwind - S01E03 - The Quiet Harbour.mkv", None),
    ("Winter Tide part1.mkv", "part1"),
    ("Northwind S03E00 Coldwater- Part II  [x265].mkv", "part2"),
    ("Northwind S03E00 Coldwater- Part I.mkv", "part1"),
    ("Northwind - S02E00.1 - Nightjar.mkv", ".1"),
    ("Northwind - S02E00.2 - Nightjar.mkv", ".2"),
    ("Northwind.S01E01.1080p.mkv", None),
    ("Winter Tide Partial.mkv", None),
    ("Winter Tide - cd2.avi", "cd2"),
    ("Winter Tide (2011).mkv", None),
])
def test_a_segment_is_read_from_the_file_name(name: str, segment: str | None) -> None:
    assert segment_of(f"/srv/media/series/x/{name}") == segment


def test_segments_of_one_episode_are_not_duplicates() -> None:
    """S01E01a, b and c share a slot and are three parts, not three copies."""
    folder = "/srv/media/series/Harbour Lights/Season 01"
    scan = find_groups([
        episode_row(10 + i, ident(100), "Harbour Lights",
                    f"{folder}/Harbour Lights - s01e01{letter}.mkv")
        for i, letter in enumerate("abc")
    ])
    (group,) = scan.groups
    assert group.cls == SEGMENTS and group.not_duplicate is not None


def test_two_copies_of_one_segment_are_still_copies() -> None:
    folder = "/srv/media/series/Harbour Lights/Season 01"
    scan = find_groups([
        episode_row(10, ident(100), "Harbour Lights", f"{folder}/Harbour Lights - S01E01a.mkv"),
        episode_row(11, ident(100), "Harbour Lights", f"{folder}/Harbour Lights - S01E01b.mkv"),
        episode_row(12, ident(100), "Harbour Lights",
                    f"{folder}/old/Harbour Lights - S01E01a.mkv"),
    ])
    classes = sorted((g.cls or "copies", len(g.members)) for g in scan.groups)
    assert classes == [("copies", 2), (SEGMENTS, 3)]
    (copies,) = scan.candidates
    assert copies.key.endswith("segment a")


def test_one_file_catalogued_twice_is_not_a_duplicate() -> None:
    scan = find_groups([
        movie_row(1, "Golden Meridian", "/srv/media/movies/Golden Meridian/g.mkv"),
        movie_row(2, "Golden Meridian", "/srv/media/movies/golden meridian/G.mkv"),
    ])
    (group,) = scan.groups
    assert group.not_duplicate is not None and "same file" in group.not_duplicate


def test_an_item_with_two_sources_is_two_copies() -> None:
    row = movie_row(1, "Blue Canyon", "/srv/media/movies/Blue Canyon/a.mkv", MediaSources=[
        {"Id": ident(1), "Path": "/srv/media/movies/Blue Canyon/a.mkv"},
        {"Id": ident(9), "Path": "/srv/media/movies/Blue Canyon/b.mkv"},
    ])
    found = members_of(row)
    assert [m.item_id for m in found] == [ident(1), ident(9)]
    assert all(m.parent_id == ident(1) for m in found)
    assert len(find_groups([row]).groups) == 1


# ===================================================================== rules
def test_a_copy_that_has_everything_replaces_one_that_has_less() -> None:
    big = copy("big", audio_tracks=(audio("eng", 6), audio("deu", 6)),
               subtitles=(subtitle("eng"), subtitle("deu")))
    small = copy("small", audio_tracks=(audio("eng", 2),), subtitles=(subtitle("eng"),), n=2)
    choice = choose([small, big], RULES)
    assert choice.verdict == SAFE and choice.keeper is big
    assert choice.losers == (small,)


def test_a_missing_audio_language_refuses() -> None:
    found = coverage(copy(audio_tracks=(audio("eng"),)),
                     copy(audio_tracks=(audio("eng"), audio("deu", 2))), RULES)
    assert not found.ok and "no deu audio" in found.missing[0]


def test_fewer_channels_in_a_language_refuses() -> None:
    found = coverage(copy(audio_tracks=(audio("eng", 2),)),
                     copy(audio_tracks=(audio("eng", 6),)), RULES)
    assert not found.ok and "2 ch" in found.missing[0]


def test_a_lossless_track_must_be_kept() -> None:
    found = coverage(copy(audio_tracks=(audio("eng", 8),)),
                     copy(audio_tracks=(audio("eng", 6, "truehd", lossless=True),)), RULES)
    assert not found.ok and "lossless" in found.missing[0]


def test_a_commentary_track_must_be_kept() -> None:
    commentary = audio("eng", 2, commentary=True, title="Director's commentary")
    found = coverage(copy(audio_tracks=(audio("eng", 6),)),
                     copy(audio_tracks=(audio("eng", 6), commentary)), RULES)
    assert not found.ok and "commentary" in found.missing[0]


def test_a_kept_subtitle_language_must_be_kept_and_its_forced_track() -> None:
    plain = coverage(copy(), copy(subtitles=(subtitle("deu"),)), RULES)
    assert not plain.ok and "no deu subtitles" in plain.missing[0]
    forced = coverage(copy(subtitles=(subtitle("eng"),)),
                      copy(subtitles=(subtitle("eng"), subtitle("eng", forced=True))), RULES)
    assert not forced.ok and "forced eng" in forced.missing[0]
    external = coverage(copy(subtitles=(subtitle("eng", forced=True, external=True),)),
                        copy(subtitles=(subtitle("eng", forced=True),)), RULES)
    assert external.ok, "a subtitle file beside the video counts"


def test_losing_other_languages_subtitles_is_allowed_and_reported() -> None:
    found = coverage(copy(subtitles=(subtitle("eng"),)),
                     copy(subtitles=(subtitle("eng"), subtitle("fra"), subtitle("ita"))),
                     RULES)
    assert found.ok
    assert found.losses == ("subtitles in fra, ita, which the policy does not keep",)


def test_with_no_kept_languages_every_subtitle_language_is_kept() -> None:
    found = coverage(copy(), copy(subtitles=(subtitle("fra"),)), Rules())
    assert not found.ok


def test_a_second_lesser_track_in_a_kept_language_may_go() -> None:
    found = coverage(copy(audio_tracks=(audio("eng", 6),)),
                     copy(audio_tracks=(audio("eng", 6), audio("eng", 2))), RULES)
    assert found.ok
    assert "1 more eng audio track(s)" in found.losses[0]


def test_a_droppable_language_may_be_lost_and_is_reported() -> None:
    rules = replace(RULES, droppable_languages=("spa",))
    found = coverage(copy(audio_tracks=(audio("eng"),)),
                     copy(audio_tracks=(audio("eng"), audio("spa", 2))), rules)
    assert found.ok and "the policy lets spa go" in found.losses[0]


def test_a_keeper_shorter_than_the_tolerance_refuses() -> None:
    assert coverage(copy(duration=5400 - 60), copy(duration=5400), RULES).ok
    short = coverage(copy(duration=5400 - 600), copy(duration=5400), RULES)
    assert not short.ok and "shorter" in short.missing[0]
    unknown = coverage(copy(duration=None), copy(), RULES)
    assert not unknown.ok


def test_copies_far_apart_in_length_are_different_cuts() -> None:
    choice = choose([copy(duration=5400), copy("b", duration=5400 + 1800, n=2)], RULES)
    assert choice.verdict == KEEP_BOTH and "different cuts" in choice.reasons[0]


def test_a_conflict_between_the_rules_keeps_both() -> None:
    """Lossless original language with stereo English, against English 5.1."""
    original = copy("original", audio_tracks=(
        audio("deu", 6, "truehd", lossless=True), audio("eng", 2)))
    english = copy("english", audio_tracks=(audio("eng", 6), audio("deu", 6)), n=2)
    choice = choose([original, english], RULES)
    assert choice.verdict == KEEP_BOTH
    assert choice.keeper is None
    text = " ".join(choice.reasons)
    assert "lossless deu" in text and "eng audio at 2 ch" in text


@pytest.mark.parametrize("criterion", ["lossless", "channels", "source", "resolution",
                                       "bitrate"])
def test_the_preference_order_decides_between_eligible_keepers(criterion: str) -> None:
    worse = copy("worse", audio_tracks=(audio("eng", 6),), origin="re-encode",
                 width=1280, height=720, bitrate=5_000_000, size=20 * 2**30)
    better_by = {
        "lossless": {"audio": (audio("eng", 6, "flac", lossless=True),)},
        "channels": {"audio": (audio("eng", 8),)},
        "source": {"origin": "source"},
        "resolution": {"width": 1920, "height": 1080},
        "bitrate": {"bitrate": 9_000_000},
    }[criterion]
    better = copy("better", n=2, audio_tracks=(audio("eng", 6),), origin="re-encode",
                  width=1280, height=720, bitrate=5_000_000, size=2**30)
    better = replace(better, **better_by)
    choice = choose([worse, better], RULES)
    assert choice.verdict == SAFE and choice.keeper is better
    assert f"for {criterion}" in choice.reasons[1]


def test_the_order_itself_is_the_owners_to_set() -> None:
    lossless_stereo_ok = copy("a", audio_tracks=(audio("eng", 6, "flac", lossless=True),),
                              width=1280, height=720)
    big_picture = copy("b", n=2, audio_tracks=(audio("eng", 6, "flac", lossless=True),),
                       width=3840, height=2160, bitrate=1)
    by_default = choose([lossless_stereo_ok, big_picture], RULES)
    assert by_default.keeper is big_picture
    policy = replace(DedupePolicy(), prefer=("bitrate", "resolution"))
    by_bitrate = choose([lossless_stereo_ok, big_picture], replace(RULES, dedupe=policy))
    assert by_bitrate.keeper is lossless_stereo_ok


def test_a_scope_crop_is_not_a_lower_resolution() -> None:
    assert resolution_class(1920, 800) == resolution_class(1920, 1080) == 1920
    assert resolution_class(1920, 872) == 1920
    assert resolution_class(1440, 1080) == 1920
    assert resolution_class(3840, 1600) == 3840
    assert resolution_class(1280, 544) == 1280 < resolution_class(1916, 1036)
    cropped = copy("cropped", width=1920, height=800, bitrate=12_000_000)
    boxed = copy("boxed", n=2, width=1920, height=1080, bitrate=8_000_000)
    choice = choose([boxed, cropped], RULES)
    assert choice.keeper is cropped, "the bitrate decides, not the letterbox"


def test_a_name_says_source_or_re_encode() -> None:
    policy = DedupePolicy()
    assert origin_of("/srv/media/movies/Winter Tide (2011) Remux/w.mkv", policy)[0] == "source"
    assert origin_of("/srv/media/movies/Winter.Tide.x264/w.mkv", policy)[0] == "re-encode"
    assert origin_of("/srv/media/movies/Remux x265/w.mkv", policy)[0] == "re-encode"
    assert origin_of("/srv/media/movies/Winter Tide/w.mkv", policy)[0] == "unknown"


def test_ties_are_noted_and_the_larger_file_kept() -> None:
    small, large = copy("small", size=1), copy("large", n=2, size=2)
    choice = choose([small, large], RULES)
    assert choice.keeper is large and "tied" in choice.notes[0]


# ---------------------------------------------------------------- the policy
def test_the_policy_is_read_from_its_own_table() -> None:
    config = loads(
        '[policy]\nkeep_languages = ["eng", "deu"]\n'
        "[policy.dedupe]\nruntime_tolerance_s = 30\nprefer = [\"channels\", \"lossless\"]\n"
        'keeper_check = "quick"\nsource_markers = ["Remux"]\n'
    )
    dedupe = config.policy.dedupe
    assert dedupe.runtime_tolerance_s == 30 and dedupe.prefer == ("channels", "lossless")
    assert dedupe.keeper_check == "quick" and dedupe.source_markers == ("remux",)
    assert Rules.from_config(config).keep_languages == ("eng", "deu")


def test_every_problem_in_the_dedupe_table_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as caught:
        loads(
            "[policy.dedupe]\nruntime_tolerance_s = -1\nprefer = [\"size\"]\n"
            'keeper_check = "none"\nsurprise = 1\n'
        )
    text = str(caught.value)
    for fragment in ("runtime_tolerance_s", "prefer", "keeper_check", "surprise"):
        assert fragment in text


# =============================================================== the resolve
def _probe_from(copies: Mapping[str, Copy]) -> Any:
    def probe(m: Member) -> Copy:
        if m.path not in copies:
            raise FileNotFoundError(f"nothing at {m.path}")
        return replace(copies[m.path], member=m)
    return probe


def _plays(path: Path) -> IntegrityReport:
    return IntegrityReport(path=Path(path), decode_errors=(),
                           notes=("a stand-in: this test is not about the payload",))


def _group_of(*copies: Copy) -> Any:
    rows = [movie_row(i + 1, "The Quiet Harbour", c.member.path) for i, c in
            enumerate(copies)]
    return find_groups(rows).groups


def test_a_keeper_whose_payload_is_not_there_blocks_its_group() -> None:
    """The rule that matters most.

    The copy the rules prefer has perfect headers and a body of zeros. It
    must not be kept on its headers' word, and the other copy -- the one
    that plays -- must not be parked.
    """
    better = copy("better", audio_tracks=(audio("eng", 6), audio("deu", 6)))
    worse = copy("worse", n=2, audio_tracks=(audio("eng", 2),))
    groups = _group_of(better, worse)

    def zeros(path: Path) -> IntegrityReport:
        return IntegrityReport(path=Path(path), problems=(
            "63 of 64 sampled blocks are nothing but zero bytes",))

    (verdict,) = resolve(groups, RULES, prober=_probe_from(
        {better.member.path: better, worse.member.path: worse}), checker=zeros)
    assert verdict.verdict == BLOCKED
    assert verdict.keeper is not None and verdict.keeper.member.path == better.member.path
    assert "failed its payload check" in verdict.reasons[-1]
    assert "zero bytes" in verdict.reasons[-1]


@pytest.mark.parametrize("answer", ["no evidence", "raises"])
def test_a_keeper_that_cannot_be_checked_blocks_its_group(answer: str) -> None:
    one, two = copy("one", audio_tracks=(audio("eng", 6),)), copy("two", n=2,
                                                                   audio_tracks=(audio("eng", 2),))

    def unreadable(path: Path) -> IntegrityReport:
        if answer == "raises":
            raise OSError("the device is not ready")
        return IntegrityReport(path=Path(path), evidence=False,
                               problems=("the demuxer could not read the file",))

    (verdict,) = resolve(_group_of(one, two), RULES, checker=unreadable,
                         prober=_probe_from({one.member.path: one, two.member.path: two}))
    assert verdict.verdict == BLOCKED


def test_a_copy_that_cannot_be_read_blocks_its_group() -> None:
    one = copy("one")
    two = copy("two", n=2)
    (verdict,) = resolve(_group_of(one, two), RULES, checker=_plays,
                         prober=_probe_from({one.member.path: one}))
    assert verdict.verdict == BLOCKED and "could not be read" in verdict.reasons[0]


def test_a_safe_group_says_its_keeper_was_decoded() -> None:
    one = copy("one", audio_tracks=(audio("eng", 6),))
    two = copy("two", n=2, audio_tracks=(audio("eng", 2),))
    (verdict,) = resolve(_group_of(one, two), RULES, checker=_plays,
                         prober=_probe_from({one.member.path: one, two.member.path: two}))
    assert verdict.verdict == SAFE
    assert verdict.notes[-1] == "the keeper's payload is there (decoded)"


def test_segments_are_reported_and_never_probed() -> None:
    folder = "/srv/media/series/Harbour Lights"
    groups = find_groups([
        episode_row(10 + i, ident(100), "Harbour Lights",
                    f"{folder}/Harbour Lights - s01e01{letter}.mkv")
        for i, letter in enumerate("ab")
    ]).groups

    def never(_m: Member) -> Copy:
        raise AssertionError("a segment was probed")

    (verdict,) = resolve(groups, RULES, prober=never, checker=_plays)
    assert verdict.verdict == NOT_DUPLICATE


def test_one_reader_per_disk_and_the_disks_side_by_side() -> None:
    copies: dict[str, Copy] = {}
    rows = []
    for n in range(8):
        disk = "one" if n % 2 else "two"
        path = f"/srv/{disk}/movies/The Quiet Harbour/{n}.mkv"
        copies[path] = replace(copy(str(n), n=n + 1), member=member(path, ident(n + 1)))
        rows.append(movie_row(n + 1, "The Quiet Harbour", path,
                              ProviderIds={"Tmdb": str(1000 + n // 2)}))
    groups = find_groups(rows).groups
    assert len(groups) == 4

    lock = threading.Lock()
    active: dict[str, int] = {"one": 0, "two": 0}
    most: dict[str, int] = {"one": 0, "two": 0}
    both = threading.Event()

    def disk_of(path: Path | str) -> str:
        return str(path).split("/")[2]

    def reading(path: str) -> None:
        disk = disk_of(path)
        with lock:
            active[disk] += 1
            most[disk] = max(most[disk], active[disk])
            if all(active.values()):
                both.set()
        time.sleep(0.02)
        with lock:
            active[disk] -= 1

    def probe(m: Member) -> Copy:
        reading(m.path)
        return replace(copies[m.path], member=m)

    def check(path: Path) -> IntegrityReport:
        reading(path.as_posix())
        return _plays(path)

    verdicts = resolve(groups, RULES, prober=probe, checker=check, device_of=disk_of)
    assert {v.verdict for v in verdicts} == {SAFE}
    assert most == {"one": 1, "two": 1}, "never two readers on one disk"
    assert both.is_set(), "the two disks were read side by side"


def test_the_gate_is_asked_before_every_read() -> None:
    one, two = copy("one", audio_tracks=(audio("eng", 6),)), copy("two", n=2)
    asked: list[str] = []

    def gate(device: str, item: object) -> None:
        asked.append(device)
        if len(asked) == 3:
            raise RuntimeError("the disk is busy")

    (verdict,) = resolve(_group_of(one, two), RULES, checker=_plays, before_each=gate,
                         prober=_probe_from({one.member.path: one, two.member.path: two}))
    assert len(asked) == 3, "two probes and one keeper check"
    assert verdict.verdict == BLOCKED and "busy" in verdict.reasons[-1]


# ================================================================== the plan
@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def _state(**fields: Any) -> dict[str, Any]:
    base = {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False,
            "IsFavorite": False}
    base.update(fields)
    return base


@pytest.fixture
def library(tmp_path: Path, server: tuple[str, Recorder]) -> dict[str, Path]:
    """Two copies of one film in two release folders, with sidecars."""
    _url, recorder = server
    root = tmp_path / "media" / "movies"
    keep_folder = root / "The Quiet Harbour (1978)"
    lose_folder = root / "The Quiet Harbour (1978) old"
    keep_folder.mkdir(parents=True)
    lose_folder.mkdir(parents=True)
    keeper = keep_folder / "The Quiet Harbour (1978).mkv"
    loser = lose_folder / "The Quiet Harbour (1978).mkv"
    keeper.write_bytes(b"the copy that is kept")
    loser.write_bytes(b"the other copy")
    (lose_folder / "The Quiet Harbour (1978).nfo").write_text("<movie/>", encoding="utf-8")
    (lose_folder / "The Quiet Harbour (1978).eng.srt").write_text("1", encoding="utf-8")
    (lose_folder / "The Quiet Harbour (1978).trickplay").mkdir()
    (lose_folder / "The Quiet Harbour (1978).trickplay" / "0.jpg").write_bytes(b"tile")
    (lose_folder / "folder.jpg").write_bytes(b"art")
    recorder.items = [
        movie_row(1, "The Quiet Harbour", str(keeper)),
        movie_row(2, "The Quiet Harbour", str(loser)),
    ]
    recorder.virtual_folders = [{"Name": "Films", "Locations": [str(root)]}]
    recorder.disk_root = tmp_path.resolve()
    return {"root": root, "keeper": keeper, "loser": loser, "lose_folder": lose_folder,
            "parked": tmp_path / "parked"}


def _copies_for(library: Mapping[str, Path]) -> dict[str, Copy]:
    keeper = copy("keep", audio_tracks=(audio("eng", 6), audio("deu", 6)),
                  subtitles=(subtitle("eng"),))
    loser = copy("lose", n=2, audio_tracks=(audio("eng", 2),), subtitles=(subtitle("eng"),))
    return {
        str(library["keeper"]): replace(keeper, size=library["keeper"].stat().st_size),
        str(library["loser"]): replace(loser, size=library["loser"].stat().st_size),
    }


def _verdicts(url: str, library: Mapping[str, Path]) -> Any:
    client = client_for(url)
    items = client.items(recursive=True, includeItemTypes="Movie")
    groups = find_groups(list(items)).groups
    return resolve(groups, RULES, prober=_probe_from(_copies_for(library)), checker=_plays)


def test_the_plan_carries_state_then_parks_then_tidies_then_notifies(
    server: tuple[str, Recorder], library: dict[str, Path]
) -> None:
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, ident(2))] = _state(
        Played=True, PlayCount=2, LastPlayedDate="2024-05-01T20:00:00.0000000Z")
    recorder.user_data[(THIRD_USER_ID, ident(2))] = _state(PlaybackPositionTicks=9_000)
    recorder.user_data[(USER_ID, ident(2))] = _state(IsFavorite=True)

    verdicts = _verdicts(url, library)
    assert [v.verdict for v in verdicts] == [SAFE]
    planned = build_plan(client_for(url), verdicts, parked=library["parked"])
    plan = planned.plan
    kinds = [step.action for step in plan.steps]
    assert kinds == ["userdata.write"] * 3 + ["dedupe.park"] + ["dedupe.park-sidecar"] * 3 \
        + ["dedupe.park-folder", "dedupe.notify"]
    assert recorder.user_data_writes == [], "planning writes nothing"
    assert library["loser"].exists()

    report = apply(plan, actions(client_for(url, dry_run=False), keeper_check=_plays))
    assert report.ok, str(report)

    # every user's state is on the keeper
    for user, expected in (
        (SECOND_USER_ID, {"Played": True, "PlayCount": 2}),
        (THIRD_USER_ID, {"PlaybackPositionTicks": 9_000}),
        (USER_ID, {"IsFavorite": True}),
    ):
        row = recorder.user_data[(user, ident(1))]
        assert {k: row[k] for k in expected} == expected
    assert recorder.user_data[(SECOND_USER_ID, ident(1))]["LastPlayedDate"].startswith(
        "2024-05-01")

    # the loser and everything of its own is parked, keeping the layout
    parked_video = parked_path(library["loser"], library["parked"])
    assert parked_video.read_bytes() == b"the other copy"
    assert not library["lose_folder"].exists(), "the emptied release folder went too"
    parked_folder = parked_path(library["lose_folder"], library["parked"])
    assert (parked_folder / "The Quiet Harbour (1978).eng.srt").is_file()
    assert (parked_folder / "The Quiet Harbour (1978).trickplay" / "0.jpg").is_file()
    assert (parked_folder / "folder.jpg").is_file()
    assert library["keeper"].read_bytes() == b"the copy that is kept"

    # one notification, naming the parked paths and never a library folder
    sent = [u["Path"] for u in recorder.notifications]
    assert sent == [str(library["loser"]), str(library["lose_folder"])]
    assert all(u["UpdateType"] == "Deleted" for u in recorder.notifications)
    assert recorder.deleted == [] and recorder.folders_deleted == []
    assert ident(2) in recorder.removed_by_scan and ident(1) not in recorder.removed_by_scan


def test_a_folder_that_still_holds_a_video_is_not_parked(
    server: tuple[str, Recorder], library: dict[str, Path]
) -> None:
    url, _recorder = server
    (library["lose_folder"] / "Blue Canyon (1998).mkv").write_bytes(b"another film")
    plan = build_plan(client_for(url), _verdicts(url, library),
                      parked=library["parked"]).plan
    assert "dedupe.park-folder" not in [s.action for s in plan.steps]


def test_a_run_that_stops_resumes_from_its_audit(
    server: tuple[str, Recorder], library: dict[str, Path], tmp_path: Path
) -> None:
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, ident(2))] = _state(Played=True, PlayCount=1)
    recorder.user_data[(THIRD_USER_ID, ident(2))] = _state(Played=True, PlayCount=3)
    plan = build_plan(client_for(url), _verdicts(url, library),
                      parked=library["parked"]).plan
    audit = tmp_path / "audit.jsonl"
    writer = client_for(url, dry_run=False)

    recorder.user_data_refused.add(ident(1))
    first = apply(plan, actions(writer, keeper_check=_plays), audit=audit)
    assert not first.ok
    assert [r.state for r in first.results][:2] == ["failed", "pending"]
    assert library["loser"].exists(), "nothing is parked before the state is carried"

    recorder.user_data_refused.clear()
    second = apply(plan, actions(writer, keeper_check=_plays), audit=audit)
    assert second.ok, str(second)
    assert not library["loser"].exists()
    events = [e["event"] for e in read_audit(audit) if e.get("step") == "carry:0001"]
    assert events == ["start", "failed", "start", "done"]

    third = apply(plan, actions(writer, keeper_check=_plays), audit=audit)
    assert third.ok and {r.state for r in third.results} == {"skipped"}


def test_a_park_refuses_while_the_state_is_not_on_the_keeper(
    server: tuple[str, Recorder], library: dict[str, Path]
) -> None:
    url, recorder = server
    recorder.user_data[(SECOND_USER_ID, ident(2))] = _state(Played=True, PlayCount=1)
    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id=ident(2), path=library["loser"], category="resolved-duplicate",
                   keeper=library["keeper"], keeper_id=ident(1))],
        allowed_categories=["resolved-duplicate"], parked=library["parked"],
        remove_rows=False, keeper_check=_plays,
    )
    (outcome,) = report.outcomes
    assert not outcome.allowed and library["loser"].exists()
    assert any("watched state" in c.name for c in outcome.refusals)


def test_a_park_refuses_when_the_keeper_changed_since_the_plan(
    server: tuple[str, Recorder], library: dict[str, Path]
) -> None:
    url, _recorder = server
    plan = build_plan(client_for(url), _verdicts(url, library),
                      parked=library["parked"]).plan
    library["keeper"].write_bytes(b"something else entirely, and longer")
    report = apply(plan, actions(client_for(url, dry_run=False), keeper_check=_plays))
    assert not report.ok and "plan again" in str(report.failed[0].error)
    assert library["loser"].exists()


def test_a_relative_parking_folder_is_on_each_files_own_volume(tmp_path: Path) -> None:
    here = tmp_path / "a" / "b.mkv"
    assert parked_path(here, "_parked") == Path(here.anchor) / "_parked" / Path(
        *here.parts[1:])
    assert parked_path(here, tmp_path / "p") == tmp_path / "p" / Path(*here.parts[1:])


def test_merging_states_takes_the_most_of_each() -> None:
    older = userdata.UserState(played=True, play_count=3,
                               last_played="2023-01-01T00:00:00Z")
    newer = userdata.UserState(play_count=1, position_ticks=500,
                               last_played="2024-01-01T00:00:00Z", favorite=True)
    merged = userdata.merge([older, newer])
    assert merged == userdata.UserState(True, 3, 500, "2024-01-01T00:00:00Z", True)
    assert userdata.carries(merged, older) and userdata.carries(merged, newer)
    assert not userdata.carries(userdata.UserState(), older)
    assert userdata.carries(userdata.UserState(), userdata.UserState())


# =================================================================== the verb
@pytest.fixture
def configured(server: tuple[str, Recorder], tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch) -> Path:
    url, _ = server
    config = tmp_path / "mediatoolkit.toml"
    config.write_text(
        f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
        f'user_id = "{USER_ID}"\n\n[policy]\nkeep_languages = ["eng", "deu"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    return config


def test_the_verb_dry_runs_writes_reports_and_applies(
    configured: Path, library: dict[str, Path], server: tuple[str, Recorder],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from jfkit.dedupe import resolver as resolve_module

    copies = _copies_for(library)
    monkeypatch.setattr(resolve_module, "probe_copy",
                        lambda m, _policy: replace(copies[m.path], member=m))
    monkeypatch.setattr(resolve_module.integrity, "check",
                        lambda path, **_: _plays(path))
    _url, recorder = server

    def run(*argv: str) -> int:
        return jfkit_cli.main(["--config", str(configured), "dedupe", "--no-gate", *argv])

    out = tmp_path / "out"
    assert run("--json", str(out / "d.json"), "--tsv", str(out / "d.tsv"),
               "--plan-out", str(out / "plan.json"), "--parked", str(library["parked"])) == 0
    printed = capsys.readouterr().out
    assert "1 SAFE" in printed and "nothing was changed" in printed
    assert library["loser"].exists() and recorder.user_data_writes == []
    document = json.loads((out / "d.json").read_text(encoding="utf-8"))
    assert document["groups"][0]["verdict"] == SAFE
    assert document["policy"]["keep_languages"] == ["eng", "deu"]
    tsv = (out / "d.tsv").read_text(encoding="utf-8").splitlines()
    assert tsv[0].startswith("group\tverdict") and len(tsv) == 3

    assert run("--apply") == 2, "--apply needs an audit"
    assert run("--apply", "--audit", str(out / "a.jsonl")) == 2, "and a parking place"
    assert library["loser"].exists()

    assert run("--plan", str(out / "plan.json"), "--apply", "--audit",
               str(out / "a.jsonl")) == 0
    assert not library["loser"].exists()
    assert parked_path(library["loser"], library["parked"]).is_file()


def test_the_verb_exits_one_when_a_group_is_blocked(
    configured: Path, library: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jfkit.dedupe import resolver as resolve_module

    copies = _copies_for(library)
    monkeypatch.setattr(resolve_module, "probe_copy",
                        lambda m, _policy: replace(copies[m.path], member=m))
    monkeypatch.setattr(
        resolve_module.integrity, "check",
        lambda path, **_: IntegrityReport(path=Path(path), problems=("empty",)))
    assert jfkit_cli.main(["--config", str(configured), "dedupe", "--no-gate"]) == 1
    printed = capsys.readouterr().out
    assert "1 BLOCKED" in printed and "0 step(s)" in printed
