"""The leftover categories of safe deletion, and the rules that name junk.

A leftover has no catalogue row of its own: a tracker note, a folder of
screenshots, a release folder whose video went long ago, a video whose
payload is gone. These tests are about the preconditions ``jfkit delete``
makes for them and about the classification rules; the sweep that proposes
them is in ``test_leftovers.py``.

The ones that matter most: every kind the owner or the server uses is
protected and can never be made junk; a candidate with a junction in it is
refused; a release subfolder -- whose own path is no item's path -- passes
at last; an unplayable file parks with its evidence and its sidecars, while
a replaced copy leaves its subtitles where they are.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any

import pytest
from jfkit.safedelete import CATEGORIES, Candidate, preconditions, safe_delete
from jfkit.safedelete.catalogue import Catalogue
from jfkit.safedelete.junk import DEFAULT_RULES, Kind, classify, load_rules
from jfkit.safedelete.leftovers import (
    CORRUPT,
    DEAD_FOLDER,
    RELEASE_JUNK,
    SAMPLE,
    release_evidence,
    title_of,
)
from mkvkit.integrity import IntegrityReport

from tests.fake_server import (
    ITEMS,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)

MOVIE = "The Quiet Harbour (1978)"
MOVIE_FILE = "The Quiet Harbour (1978).mkv"
SERIES_FILE = "Northwind.S01E03.1080p.WEB-DL.x264-EXAMPLE.mkv"


def ident(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


def write(path: Path, data: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def movie_row(n: int, name: str, path: Path | None, year: int = 1978,
              **extra: Any) -> dict[str, Any]:
    return {"Id": ident(n), "Name": name, "Type": "Movie", "ProductionYear": year,
            "Path": None if path is None else str(path), "ProviderIds": {},
            "UserData": {"PlayCount": 0, "PlaybackPositionTicks": 0, "Played": False,
                         "IsFavorite": False}, **extra}


# --------------------------------------------------------------- the rules
@pytest.mark.parametrize(("relative", "kind"), [
    # junk, only where a rule names it
    ("Film/EZTV.txt", Kind.JUNK),
    ("Film/Torrent downloaded from example.txt", Kind.JUNK),
    ("Film/www.example.org.txt", Kind.JUNK),
    ("Film/visit the tracker.url", Kind.JUNK),
    ("Film/codec pack.website", Kind.JUNK),
    ("Film/setup.exe", Kind.JUNK),
    ("Film/play.lnk", Kind.JUNK),
    ("Film/_____padding_file_0_do not open", Kind.JUNK),
    ("Film/.pad/0", Kind.JUNK),
    ("Film/Screens/shot01.jpg", Kind.JUNK),
    ("Film/Proof/anything.png", Kind.JUNK),
    ("Film/film-screenshot3.png", Kind.JUNK),
    ("Film/release.sfv", Kind.JUNK),
    # never junk: what the server or the owner uses
    ("Film/film.nfo", Kind.PROTECTED),
    ("Film/folder.jpg", Kind.PROTECTED),
    ("Film/poster.png", Kind.PROTECTED),
    ("Film/backdrop2.jpg", Kind.PROTECTED),
    ("Film/landscape.jpg", Kind.PROTECTED),
    ("Film/logo.png", Kind.PROTECTED),
    ("Film/banner.jpg", Kind.PROTECTED),
    ("Film/fanart.jpg", Kind.PROTECTED),
    ("Film/film-thumb.jpg", Kind.PROTECTED),
    ("Show/season01-poster.jpg", Kind.PROTECTED),
    ("Show/season-specials-banner.jpg", Kind.PROTECTED),
    ("Film/Screens/poster.jpg", Kind.PROTECTED),
    ("Film/film.trickplay/320 - 10x10/0.jpg", Kind.PROTECTED),
    ("Film/film.eng.srt", Kind.PROTECTED),
    ("Film/Subs/2_English.idx", Kind.PROTECTED),
    ("Film/samples/film.srt", Kind.PROTECTED),
    ("Film/theme.mp3", Kind.PROTECTED),
    ("Show/theme-music/opening.flac", Kind.PROTECTED),
    ("Film/film.deu.ac3", Kind.PROTECTED),
    ("Show/Soundtrack/01.flac", Kind.PROTECTED),
    ("Show/Guide/episodes.xlsx", Kind.PROTECTED),
    ("Show/Extras/comics.cbz", Kind.PROTECTED),
    ("Film/Subs/subs.rar", Kind.PROTECTED),
    ("Film/film.chapters.xml", Kind.PROTECTED),
    ("Film/BDMV/STREAM/00000.m2ts", Kind.PROTECTED),
    ("Film/BDMV/index.bdmv", Kind.PROTECTED),
    ("Film/.ignore", Kind.PROTECTED),
    ("Film/metadata/film.jpg", Kind.PROTECTED),
    ("Show/extrafanart/fanart1.jpg", Kind.PROTECTED),
    # videos and samples
    (f"Film/{MOVIE_FILE}", Kind.VIDEO),
    ("Film/Sample/film-sample.mkv", Kind.SAMPLE),
    ("Film/sample.mkv", Kind.SAMPLE),
    ("Film/The.Quiet.Harbour.sample.mkv", Kind.SAMPLE),
    ("Film/quiet.harbour.sample-example.mkv", Kind.SAMPLE),
    # unsure: never junk, only reported
    ("Film/notes.txt", Kind.UNSURE),
    ("Film/Box Cover/front.jpg", Kind.UNSURE),
    ("Film/film.iso.nojellyfin", Kind.UNSURE),
    ("Film/.0a1b2c.parts", Kind.UNSURE),
    ("Film/strange.bin2", Kind.UNSURE),
    # ignored
    ("Film/desktop.ini", Kind.IGNORED),
    ("Film/Thumbs.db", Kind.IGNORED),
])
def test_every_kind_is_classified(relative: str, kind: Kind) -> None:
    assert classify(PurePath(relative)).kind is kind, classify(PurePath(relative))


def test_no_rule_can_make_a_protected_kind_junk(tmp_path: Path) -> None:
    """Widening the tracker notes to everything still leaves protection first."""
    rules_file = write(tmp_path / "rules.toml", (
        '[leftovers]\ntracker_notes = ["*"]\nextra_screenshot_folders = ["Box Cover"]\n'
        'extra_program_suffixes = [".srt"]\n'
    ))
    rules = load_rules(rules_file)
    assert classify(PurePath("Film/notes.txt"), rules=rules).kind is Kind.JUNK
    assert classify(PurePath("Film/Box Cover/front.jpg"), rules=rules).kind is Kind.JUNK
    assert classify(PurePath("Film/film.eng.srt"), rules=rules).kind is Kind.PROTECTED
    assert classify(PurePath("Film/Box Cover/poster.jpg"), rules=rules).kind \
        is Kind.PROTECTED
    assert "*eztv*.txt" in DEFAULT_RULES.tracker_notes


def test_a_rules_file_with_an_unknown_key_is_refused(tmp_path: Path) -> None:
    rules_file = write(tmp_path / "rules.toml", '[leftovers]\ntrackers = ["*.txt"]\n')
    with pytest.raises(ValueError, match="unknown key"):
        load_rules(rules_file)


def test_every_leftover_category_says_what_it_means() -> None:
    for category in (RELEASE_JUNK, DEAD_FOLDER, CORRUPT, SAMPLE):
        assert CATEGORIES[category]


def test_the_title_and_year_of_a_release_name() -> None:
    assert title_of("The.Quiet.Harbour.1978.1080p.BluRay.x264-SAMPLE.mkv") == (
        "the quiet harbour", 1978)
    assert title_of("Northwind (2019) S02E05 Pilot [1080p]") == ("northwind", 2019)
    assert title_of("Harbour Lights") == ("harbour lights", None)
    assert title_of("Winter.Tide.German.DL.1080p.WebHD.h264-EXAMPLE") == (
        "winter tide", None)


def test_evidence_finds_an_episode_whose_file_is_missing(tmp_path: Path) -> None:
    """A release folder of one episode, and the episode's row has no path."""
    folder = write(tmp_path / "Northwind (2019) S02E05 Pilot" / "folder.jpg").parent
    row = {"Id": ident(20), "Type": "Episode", "Name": "Pilot", "SeriesName": "Northwind",
           "ParentIndexNumber": 2, "IndexNumber": 5, "Path": None}
    evidence = release_evidence(folder, Catalogue.from_rows([row]),
                                files=[folder / "folder.jpg"])
    assert evidence.status == "no-file"
    assert "its file is missing" in evidence.lines()[0]




# ---------------------------------------------------------------- junctions
def junction(target: Path, link: Path) -> None:
    import _winapi  # type: ignore[import-not-found]

    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are an NTFS feature")
def test_a_candidate_holding_a_junction_is_refused(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _recorder = server
    dead = tmp_path / "Films" / "Hollow.Lantern.2004.720p"
    write(dead / "movie.nfo", "<movie/>")
    elsewhere = write(tmp_path / "another-disk" / "The Longest Night.mkv", b"far").parent
    junction(elsewhere, dead / "pointer")
    for category in (DEAD_FOLDER, RELEASE_JUNK, "media-free-folder"):
        report = safe_delete(
            client_for(url, dry_run=False),
            [Candidate(item_id="", path=dead, category=category)],
            allowed_categories=[category], parked=tmp_path / "parked",
            catalogue=Catalogue.from_rows([]),
        )
        assert report.refused and not report.allowed, category
    assert (elsewhere / "The Longest Night.mkv").is_file()
    assert dead.is_dir()
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=dead / "pointer",
                                   category=DEAD_FOLDER),
        allowed_categories=[DEAD_FOLDER], catalogue=Catalogue.from_rows([]), roots=[],
    )
    assert any(c.name == "it is not a link or a junction" and not c.ok for c in checks)


# ------------------------------------------------- finding 2: the subfolder
def test_a_release_subfolder_is_proposable_without_an_item(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    """A screenshot folder beside a watched film: no row of its own, and it goes."""
    url, recorder = server
    folder = tmp_path / "Films" / "The Quiet Harbour (1978) BluRay"
    film = write(folder / MOVIE_FILE, b"a film")
    write(folder / "Proof" / "proof01.jpg")
    recorder.items[0]["Path"] = str(film)
    recorder.user_data[(USER_ID, recorder.items[0]["Id"])] = {"PlayCount": 3}

    for category in (RELEASE_JUNK, "media-free-folder"):
        dry = safe_delete(
            client_for(url), [Candidate(item_id="", path=folder / "Proof",
                                        category=category)],
            allowed_categories=[category], parked=tmp_path / "parked",
        )
        assert dry.allowed, str(dry)
    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id="", path=folder / "Proof", category=RELEASE_JUNK)],
        allowed_categories=[RELEASE_JUNK], parked=tmp_path / "parked",
    )
    assert report.allowed and not (folder / "Proof").exists()
    assert film.is_file()
    assert recorder.deleted == [] and recorder.notifications == []


def test_a_leftover_with_a_catalogued_file_below_it_is_refused(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, recorder = server
    folder = tmp_path / "Films" / "Blue Canyon (1998)"
    film = write(folder / "Blue Canyon (1998).nfo", "<movie/>")
    recorder.items[1]["Path"] = str(folder / "Blue Canyon (1998).mkv")
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=folder, category=DEAD_FOLDER),
        allowed_categories=[DEAD_FOLDER],
    )
    [refusal] = [c for c in checks if not c.ok]
    assert refusal.name == "nothing catalogued is at or below it"
    assert film.is_file()


def test_a_protected_file_is_refused_as_junk(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _ = server
    for name in ("film.nfo", "poster.jpg", "film.eng.srt", "theme.mp3", "film.deu.ac3",
                 "guide.pdf", "comics.cbz", "notes.txt"):
        path = write(tmp_path / "Films" / "Winter Tide (2011)" / name)
        checks, _ = preconditions(
            client_for(url), Candidate(item_id="", path=path, category=RELEASE_JUNK),
            allowed_categories=[RELEASE_JUNK], catalogue=Catalogue.from_rows([]),
        )
        assert not all(c.ok for c in checks), name


def test_a_dead_folder_holding_a_subtitle_is_refused(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _ = server
    dead = tmp_path / "Films" / "Hollow.Lantern.2004"
    write(dead / "movie.nfo", "<movie/>")
    write(dead / "Subs" / "movie.eng.srt", "1\n")
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=dead, category=DEAD_FOLDER),
        allowed_categories=[DEAD_FOLDER], catalogue=Catalogue.from_rows([]), roots=[],
    )
    [refusal] = [c for c in checks if not c.ok]
    assert "a subtitle" in refusal.detail


# ------------------------------------------------------- corrupt-unplayable
def failed(path: Path) -> IntegrityReport:
    return IntegrityReport(
        path=path, size=path.stat().st_size,
        problems=("63 of 64 sampled block(s) of 1048576 bytes are all zeros (98%)",),
    )


def _episode_with_sidecars(tmp_path: Path) -> tuple[Path, list[Path]]:
    folder = tmp_path / "Series" / "Harbour Lights" / "Season 02"
    video = write(folder / "Harbour Lights - S02E05.mkv", b"\0" * 64)
    sidecars = [
        write(folder / "Harbour Lights - S02E05.nfo", "<episodedetails/>"),
        write(folder / "Harbour Lights - S02E05-thumb.jpg"),
        write(folder / "Harbour Lights - S02E05.eng.srt", "1\n"),
        write(folder / "Harbour Lights - S02E05.trickplay" / "320 - 10x10" / "0.jpg"),
    ]
    write(folder / "Harbour Lights - S02E06.mkv", b"the next one")
    return video, [sidecars[0], sidecars[1], sidecars[2], sidecars[3].parent.parent]


def test_an_unplayable_file_parks_with_its_sidecars_and_evidence(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, recorder = server
    video, sidecars = _episode_with_sidecars(tmp_path)
    recorder.items[7]["Path"] = str(video)
    audit = tmp_path / "audit.log"
    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id=recorder.items[7]["Id"], path=video, category=CORRUPT)],
        allowed_categories=[CORRUPT], parked=tmp_path / "parked", audit=audit,
        integrity_check=failed,
    )
    assert report.allowed, str(report)
    assert not video.exists()
    for sidecar in sidecars:
        assert not sidecar.exists(), sidecar
        assert (tmp_path / "parked" / Path(*sidecar.parts[1:])).exists()
    assert (video.parent / "Harbour Lights - S02E06.mkv").is_file()
    text = audit.read_text(encoding="utf-8")
    assert "all zeros (98%)" in text, "the evidence is in the audit"
    assert "parked sidecar (trickplay)" in text
    assert recorder.removed_by_scan == [recorder_id(recorder, 7)] or \
        recorder.find(recorder_id(recorder, 7)) is None


def recorder_id(recorder: Recorder, index: int) -> str:
    return str(ITEMS[index]["Id"])


def test_a_superseded_copy_parks_its_pictures_and_keeps_its_tracks(
    server: tuple[str, Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy removed because another stays: its subtitle may be the only one."""
    from jfkit import safedelete as safedelete_module

    monkeypatch.setattr(safedelete_module, "default_keeper_check",
                        lambda *, decode=True: lambda p: IntegrityReport(path=Path(p)))
    url, recorder = server
    video, sidecars = _episode_with_sidecars(tmp_path)
    keeper = write(tmp_path / "Series" / "Harbour Lights" / "Harbour Lights S02E05.mkv",
                   b"the copy that plays")
    recorder.items[7]["Path"] = str(video)
    recorder.items[6]["Path"] = str(keeper)
    report = safe_delete(
        client_for(url, dry_run=False),
        [Candidate(item_id=recorder.items[7]["Id"], path=video,
                   category="superseded-copy", keeper_id=recorder.items[6]["Id"])],
        allowed_categories=["superseded-copy"], parked=tmp_path / "parked",
    )
    assert report.allowed, str(report)
    nfo, thumb, subtitle, tiles = sidecars
    assert not nfo.exists() and not thumb.exists() and not tiles.exists()
    assert subtitle.is_file(), "a track is never taken with a copy that is replaced"
    assert any("may be the only copy" in n for n in report.allowed[0].notes)


def test_an_unplayable_file_with_a_copy_is_refused(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _ = server
    video, _ = _episode_with_sidecars(tmp_path)
    other = write(tmp_path / "Series" / "Harbour Lights" / "S02E05 again.mkv", b"plays")
    rows = [
        {"Id": ident(30), "Type": "Episode", "Name": "x", "SeriesId": ident(29),
         "ParentIndexNumber": 2, "IndexNumber": 5, "Path": str(video)},
        {"Id": ident(31), "Type": "Episode", "Name": "x", "SeriesId": ident(29),
         "ParentIndexNumber": 2, "IndexNumber": 5, "Path": str(other)},
    ]
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=video, category=CORRUPT),
        allowed_categories=[CORRUPT], catalogue=Catalogue.from_rows(rows),
        integrity_check=failed,
    )
    [refusal] = [c for c in checks if not c.ok]
    assert refusal.name == "no other copy of it is catalogued"
    assert "superseded-copy" in refusal.detail


@pytest.mark.parametrize("report", [
    IntegrityReport(path=Path("x"), evidence=False, problems=("ffprobe is missing",)),
    IntegrityReport(path=Path("x")),
])
def test_no_evidence_or_a_passing_check_is_refused(
    server: tuple[str, Recorder], tmp_path: Path, report: IntegrityReport,
) -> None:
    url, _ = server
    video, _ = _episode_with_sidecars(tmp_path)
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=video, category=CORRUPT,
                                   integrity=report),
        allowed_categories=[CORRUPT], catalogue=Catalogue.from_rows([]),
    )
    [refusal] = [c for c in checks if not c.ok]
    assert refusal.name == "integrity evidence says it does not play"


def test_a_file_that_changed_since_it_was_measured_is_refused(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _ = server
    video, _ = _episode_with_sidecars(tmp_path)
    before = video.stat()
    video.write_bytes(b"\0" * 128)
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=video, category=CORRUPT,
                                   integrity=failed(video),
                                   measured=(before.st_size, before.st_mtime_ns)),
        allowed_categories=[CORRUPT], catalogue=Catalogue.from_rows([]),
    )
    assert [c.name for c in checks if not c.ok] == [
        "it has not changed since it was measured"]


@pytest.mark.needs_ffmpeg
def test_a_zero_filled_file_is_measured_unplayable_for_real(
    server: tuple[str, Recorder], tmp_path: Path, media_fixtures: dict[str, Path],
) -> None:
    from tests.test_safedelete import zeroed_copy

    url, _ = server
    video = tmp_path / "Series" / "Harbour Lights" / "Harbour Lights - S02E05.mkv"
    video.parent.mkdir(parents=True)
    zeroed_copy(media_fixtures["tiny_multitrack.mkv"], video)
    checks, _ = preconditions(
        client_for(url), Candidate(item_id="", path=video, category=CORRUPT),
        allowed_categories=[CORRUPT], catalogue=Catalogue.from_rows([]),
    )
    assert all(c.ok for c in checks), [str(c) for c in checks]


# ------------------------------------------------------------------ the verb
def test_delete_takes_a_leftover_manifest_with_no_item(
    server: tuple[str, Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from jfkit import cli as jfkit_cli

    from tests.fake_server import CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL

    url, recorder = server
    folder = tmp_path / "Films" / "Winter Tide (2011)"
    film = write(folder / "Winter Tide (2011).mkv", b"film")
    note = write(folder / "EZTV.txt", "tracker")
    recorder.items[2]["Path"] = str(film)
    manifest = write(tmp_path / "manifest.json", json.dumps([
        {"item_id": "", "path": str(note), "category": RELEASE_JUNK},
        {"item_id": "", "path": str(film), "category": RELEASE_JUNK},
    ]))
    config = write(tmp_path / "mediatoolkit.toml", (
        f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
        f'user_id = "{USER_ID}"\n'))
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    code = jfkit_cli.main(["--config", str(config), "delete", str(manifest),
                           "--release", RELEASE_JUNK, "--parked", str(tmp_path / "p"),
                           "--apply"])
    out = capsys.readouterr().out
    assert code == 1, "the film is refused, so the run says so"
    assert "1 would be parked, 1 refused" in out or "1 refused" in out
    assert not note.exists() and film.is_file()


def test_the_health_manifest_is_read_as_the_corrupt_category(tmp_path: Path) -> None:
    """``mkvkit health --manifest`` writes rows ``jfkit delete`` reads as
    ``corrupt-unplayable``: one name for one category on both sides."""
    from jfkit.safedelete import load_manifest
    from mkvkit.health import CORRUPT as CONFIRMED
    from mkvkit.health import MANIFEST_CATEGORY, FileResult, manifest_rows, manifest_text

    assert MANIFEST_CATEGORY == CORRUPT
    manifest = tmp_path / "corrupt.tsv"
    manifest.write_text(manifest_text(manifest_rows([
        FileResult(str(tmp_path / "a.mkv"), 1, 1, str(tmp_path), CONFIRMED, stage=2,
                   evidence=("stage 2: packets for 2 s",),
                   item_id="00000000-0000-0000-0000-000000000001"),
    ])), encoding="utf-8")
    (candidate,) = load_manifest(manifest)
    assert candidate.category == CORRUPT and candidate.category in CATEGORIES
    assert candidate.item_id == "00000000-0000-0000-0000-000000000001"
    assert candidate.path == tmp_path / "a.mkv"
