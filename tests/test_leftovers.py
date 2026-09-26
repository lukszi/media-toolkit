"""Leftovers: release junk, dead release folders, samples and unplayable files.

A sweep walks the library folders, sorts every file into one class, and
proposes the leftovers. Nothing is deletable by default and nothing is ever
deleted: a proposal passes the same preconditions ``jfkit delete`` makes,
and only a released category is parked, by a plan that checks each
candidate again as it runs.

The tests that matter most: a folder with a junction in it is never
proposed, and refused if proposed anyway; every step checks its candidate
again; a run that stopped resumes; and the missing-content report finds
each kind of absence and never calls unwalked ground empty. The categories
and the rules themselves are in ``test_safedelete_leftovers.py``.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path, PurePath
from typing import Any

import pytest
from jfkit import cli as jfkit_cli
from jfkit.leftovers import (
    Finding,
    actions,
    assess,
    build_plan,
    find_missing,
    park_target,
    scan,
    scan_root,
)
from jfkit.leftovers import plan as plan_module
from jfkit.leftovers.missing import render
from jfkit.safedelete import Candidate, safe_delete
from jfkit.safedelete.catalogue import Catalogue, path_key
from jfkit.safedelete.leftovers import CORRUPT, DEAD_FOLDER, RELEASE_JUNK, SAMPLE
from mkvkit.steps import apply, read_audit

from tests.fake_server import (
    CREDENTIAL_VARIABLE,
    FIXTURE_CREDENTIAL,
    USER_ID,
    Recorder,
    client_for,
    fake_server,
)
from tests.test_safedelete_leftovers import MOVIE_FILE, ident, junction, movie_row, write


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


# ---------------------------------------------------------------- the tree
@pytest.fixture
def library(tmp_path: Path) -> dict[str, Any]:
    """A films folder with one live release, its clutter, and three dead ones."""
    root = tmp_path / "Films"
    live = root / "The Quiet Harbour (1978) BluRay"
    video = write(live / MOVIE_FILE, b"a film")
    write(live / "The Quiet Harbour (1978) BluRay.nfo", "<movie/>")
    write(live / "poster.jpg")
    write(live / "EZTV.txt", "tracker")
    write(live / "visit.url", "[InternetShortcut]")
    write(live / "Proof" / "proof01.jpg")
    write(live / "Screens" / "shot01.png")
    write(live / "Subs" / "film.eng.srt", "1\n")
    write(live / "Sample" / "film-sample.mkv", b"a sample")
    write(live / "notes.txt", "somebody's notes")
    write(live / ".pad" / "0", b"\0")
    write(live / "BDMV" / "index.bdmv")

    dead = root / "Blue.Canyon.1998.old.release"
    write(dead / "Blue.Canyon.1998.old.release.nfo",
          "<movie><imdbid>tt0000002</imdbid></movie>")
    write(dead / "cover.jpg")
    write(dead / "release.sfv")

    tiles = root / "winter-tide-2011"
    write(tiles / "winter-tide-2011.trickplay" / "320 - 10x10" / "0.jpg")

    lost = root / "Hollow.Lantern.2004.old.release"
    write(lost / "Hollow.Lantern.2004.nfo", "<movie/>")
    write(lost / "folder.jpg")

    soundtrack = root / "Golden Meridian (1987)" / "Soundtrack"
    write(soundtrack / "01.flac")
    write(root / "Golden Meridian (1987)" / "Golden Meridian (1987).mkv", b"film")

    elsewhere = root / "Blue Canyon (1998)" / "Blue Canyon (1998).mkv"
    write(elsewhere, b"another film")
    tide = write(root / "Winter Tide (2011).avi", b"a loose film in the root")
    rows = [
        movie_row(1, "The Quiet Harbour", video),
        movie_row(2, "Blue Canyon", elsewhere, 1998, ProviderIds={"Imdb": "tt0000002"}),
        movie_row(3, "Winter Tide", tide, 2011),
        movie_row(4, "Golden Meridian", root / "Golden Meridian (1987)" /
                  "Golden Meridian (1987).mkv", 1987),
    ]
    return {"root": root, "live": live, "dead": dead, "tiles": tiles, "lost": lost,
            "soundtrack": soundtrack, "rows": rows, "video": video}


def by_path(found: Any) -> dict[Path, Any]:
    return {f.path: f for f in found}


def test_the_sweep_sorts_a_library(library: dict[str, Any]) -> None:
    catalogue = Catalogue.from_rows(library["rows"])
    found = scan_root(library["root"], catalogue)
    findings = by_path(found.findings)
    live, dead = library["live"], library["dead"]

    assert findings[live / "EZTV.txt"].category == RELEASE_JUNK
    assert findings[live / "visit.url"].category == RELEASE_JUNK
    assert findings[live / "Proof"].category == RELEASE_JUNK
    assert findings[live / "Proof"].is_dir
    assert findings[live / "Screens"].category == RELEASE_JUNK
    assert findings[live / ".pad"].category == RELEASE_JUNK
    assert findings[live / "Sample" / "film-sample.mkv"].category == SAMPLE
    assert findings[dead].category == DEAD_FOLDER
    assert findings[library["tiles"]].category == DEAD_FOLDER
    assert findings[library["lost"]].category == DEAD_FOLDER
    # the evidence: catalogued elsewhere by the id its description file names
    assert findings[dead].evidence.status == "elsewhere"
    assert "imdb id tt0000002" in findings[dead].evidence.lines()[0]
    assert findings[library["lost"]].evidence.status == "unmatched"
    # never proposed
    never = {live / MOVIE_FILE, live / "poster.jpg", live / "Subs",
             live / "Subs" / "film.eng.srt", live / "BDMV", library["soundtrack"],
             live / "The Quiet Harbour (1978) BluRay.nfo"}
    assert not never & set(findings)
    kept = by_path(found.kept)
    assert kept[live / "notes.txt"].kind == "unsure"
    assert kept[live / "Subs"].kind == "kept-folder"
    assert "a subtitle" in kept[live / "Subs"].reason
    assert "an audio track" in kept[library["soundtrack"]].reason
    # files inside a proposed folder are not proposed twice
    assert live / "Proof" / "proof01.jpg" not in findings


def test_pictures_beside_a_live_video_are_not_a_dead_folder(tmp_path: Path) -> None:
    root = tmp_path / "Films"
    film = write(root / "Winter Tide (2011)" / "Winter Tide (2011).mkv", b"film")
    write(root / "Winter Tide (2011)" / "Cover Art" / "front-cover.jpg")
    write(root / "Winter Tide (2011)" / "Cover Art" / "poster.jpg")
    catalogue = Catalogue.from_rows([movie_row(3, "Winter Tide", film, 2011)])
    found = scan_root(root, catalogue)
    assert not found.findings
    [kept] = found.kept_folders
    assert "beside a video" in kept.reason or "neither artwork" in kept.reason


def test_a_catalogued_folder_is_never_a_leftover(tmp_path: Path) -> None:
    """A series row whose folder holds only artwork is catalogued, not dead."""
    root = tmp_path / "Series"
    show = root / "Signal Hill"
    write(show / "folder.jpg")
    write(show / "tvshow.nfo", "<tvshow/>")
    catalogue = Catalogue.from_rows([{"Id": ident(6), "Type": "Series",
                                      "Name": "Signal Hill", "Path": str(show)}])
    assert not scan_root(root, catalogue).findings


def test_a_folder_row_does_not_count_as_catalogued(tmp_path: Path) -> None:
    root = tmp_path / "Films"
    dead = root / "Hollow.Lantern.2004.720p"
    write(dead / "movie.nfo", "<movie/>")
    catalogue = Catalogue.from_rows([{"Id": ident(9), "Type": "Folder",
                                      "Name": "x", "Path": str(dead)}])
    assert by_path(scan_root(root, catalogue).findings)[dead].category == DEAD_FOLDER



@pytest.mark.skipif(sys.platform != "win32", reason="junctions are an NTFS feature")
def test_a_junction_inside_a_candidate_stops_it(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, _recorder = server
    root = tmp_path / "Films"
    dead = root / "Hollow.Lantern.2004.720p"
    write(dead / "movie.nfo", "<movie/>")
    elsewhere = write(tmp_path / "another-disk" / "The Longest Night.mkv", b"far").parent
    junction(elsewhere, dead / "pointer")
    junction(elsewhere, root / "Pointer To Another Disk")
    catalogue = Catalogue.from_rows([])

    found = scan_root(root, catalogue)
    assert dead not in by_path(found.findings), "a folder with a junction is not proposed"
    assert {s.path for s in found.skipped} == {dead / "pointer",
                                                root / "Pointer To Another Disk"}
    assert not any("another-disk" in str(k.path) for k in found.kept)

    # proposed by hand anyway, it is refused, and nothing behind it is touched
    for category in (DEAD_FOLDER, RELEASE_JUNK):
        report = safe_delete(
            client_for(url, dry_run=False),
            [Candidate(item_id="", path=dead, category=category)],
            allowed_categories=[category], parked=tmp_path / "parked",
            catalogue=catalogue,
        )
        assert report.refused and not report.allowed
        assert any("link, junction" in c.name for c in report.refused[0].refusals)
    assert (elsewhere / "The Longest Night.mkv").is_file()
    assert dead.is_dir()



# ------------------------------------------------------------ plan and apply
def _sweep(server: tuple[str, Recorder], library: dict[str, Any], *released: str,
           dry_run: bool = True) -> tuple[Any, Any, Any]:
    url, recorder = server
    recorder.items[:] = library["rows"]
    recorder.virtual_folders = [{"Name": "Films", "Locations": [str(library["root"])]}]
    client = client_for(url, dry_run=dry_run)
    catalogue = Catalogue.fetch(client)
    found = scan([library["root"]], catalogue)
    assessed = assess(client, found.findings, released=released, catalogue=catalogue,
                      roots=[str(library["root"])])
    return client, found, assessed


def test_nothing_is_planned_without_a_release(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
) -> None:
    _client, _found, assessed = _sweep(server, library)
    plan = build_plan(assessed, parked=tmp_path / "parked")
    assert plan.steps == ()
    states = {a.finding.path: a.state for a in assessed}
    assert states[library["live"] / "EZTV.txt"] == "not released"
    assert states[library["dead"]] == "not released"


def test_samples_move_only_when_their_category_is_released(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
) -> None:
    _client, _found, assessed = _sweep(server, library, RELEASE_JUNK, DEAD_FOLDER)
    plan = build_plan(assessed, parked=tmp_path / "parked")
    moved = {step.params["category"] for step in plan.steps}
    assert moved == {RELEASE_JUNK, DEAD_FOLDER}
    _client, _found, assessed = _sweep(server, library, SAMPLE)
    assert {s.params["category"] for s in build_plan(assessed, parked=tmp_path).steps} \
        == {SAMPLE}


def test_an_applied_plan_parks_everything_and_touches_nothing_else(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
) -> None:
    client, _found, assessed = _sweep(server, library, RELEASE_JUNK, DEAD_FOLDER,
                                      dry_run=False)
    parked = tmp_path / "parked"
    plan = build_plan(assessed, parked=parked)
    report = apply(plan, actions(client), audit=tmp_path / "audit.jsonl")
    assert report.ok, str(report)
    live = library["live"]
    for gone in (live / "EZTV.txt", live / "Proof", library["dead"], library["tiles"]):
        assert not gone.exists(), gone
        assert park_target(parked, gone).exists(), gone
    for kept in (library["video"], live / "poster.jpg", live / "Subs" / "film.eng.srt",
                 live / "notes.txt", live / "Sample" / "film-sample.mkv",
                 library["soundtrack"] / "01.flac"):
        assert kept.exists(), kept
    _url, recorder = server
    assert recorder.deleted == []


def test_each_step_checks_its_candidate_again(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
) -> None:
    """A row that appears after the plan was made stops the step that would hit it."""
    client, _found, assessed = _sweep(server, library, DEAD_FOLDER, dry_run=False)
    plan = build_plan(assessed, parked=tmp_path / "parked")
    _url, recorder = server
    recorder.items.append(movie_row(40, "Hollow Lantern",
                                    library["lost"] / "Hollow.Lantern.2004.mkv", 2004))
    ordered = sorted(plan.steps, key=lambda s: s.params["src"] != str(library["lost"]))
    first = type(plan)(verb=plan.verb, steps=tuple(ordered), notes=plan.notes)
    report = apply(first, actions(client), audit=tmp_path / "audit.jsonl")
    assert not report.ok
    assert "nothing catalogued is at or below it" in report.failed[0].error
    assert library["lost"].is_dir()


def test_a_run_that_stopped_resumes_where_it_stopped(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _found, assessed = _sweep(server, library, RELEASE_JUNK, dry_run=False)
    plan = build_plan(assessed, parked=tmp_path / "parked")
    audit = tmp_path / "audit.jsonl"
    real = plan_module._park_file
    calls = {"n": 0}

    def flaky(src: Path, dst: Path) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("held open by another program")
        return real(src, dst)

    monkeypatch.setattr(plan_module, "_park_file", flaky)
    first = apply(plan, actions(client), audit=audit)
    assert not first.ok
    monkeypatch.setattr(plan_module, "_park_file", real)
    second = apply(plan, actions(client), audit=audit)
    assert second.ok, str(second)
    states = [r.state for r in second.results]
    assert states[0] == "skipped" and all(s in ("done", "skipped") for s in states)
    events = [e["event"] for e in read_audit(audit)]
    assert events.count("plan") == 2 and "failed" in events


def test_a_folder_half_parked_is_finished_not_duplicated(
    server: tuple[str, Recorder], tmp_path: Path,
) -> None:
    url, recorder = server
    root = tmp_path / "Films"
    screens = root / "Winter Tide (2011)" / "Screens"
    write(root / "Winter Tide (2011)" / "Winter Tide (2011).mkv", b"film")
    one, two = write(screens / "a.png", b"one"), write(screens / "b.png", b"two")
    recorder.items[:] = [movie_row(3, "Winter Tide",
                                   root / "Winter Tide (2011)" / "Winter Tide (2011).mkv")]
    recorder.virtual_folders = [{"Name": "Films", "Locations": [str(root)]}]
    client = client_for(url, dry_run=False)
    catalogue = Catalogue.fetch(client)
    parked = tmp_path / "parked"
    assessed = assess(client, scan([root], catalogue).findings, released=[RELEASE_JUNK],
                      catalogue=catalogue)
    plan = build_plan(assessed, parked=parked)
    write(park_target(parked, one), b"one")  # an earlier run got this far
    assert apply(plan, actions(client)).ok
    assert not screens.exists()
    assert park_target(parked, two).read_bytes() == b"two"


def test_a_park_across_volumes_is_a_verified_copy(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _found, assessed = _sweep(server, library, DEAD_FOLDER, dry_run=False)
    copies: list[Path] = []
    real = plan_module.verified_copy

    def spy(src: Any, dst: Any, **kwargs: Any) -> Any:
        copies.append(Path(src))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(plan_module, "same_device", lambda a, b: False)
    monkeypatch.setattr(plan_module, "verified_copy", spy)
    plan = build_plan(assessed, parked=tmp_path / "parked")
    assert apply(plan, actions(client)).ok
    assert copies and not library["dead"].exists()


def test_the_parking_place_keeps_the_drive() -> None:
    assert park_target("/srv/parked", PurePath("/srv/media/movies/x.txt")) == Path(
        "/srv/parked/srv/media/movies/x.txt")
    if sys.platform == "win32":
        assert park_target("/srv/parked", "C:/Media/Movies/x.txt") == Path(
            "/srv/parked/C/Media/Movies/x.txt")


# ------------------------------------------------------------- the report
def test_every_kind_of_missing_content_is_reported(
    library: dict[str, Any], tmp_path: Path,
) -> None:
    root = library["root"]
    series = tmp_path / "Series" / "Northwind"
    rows = [
        *library["rows"],
        movie_row(50, "Hollow Lantern", root / "Hollow Lantern (2004).mkv", 2004),
        {"Id": ident(60), "Type": "Series", "Name": "Northwind", "Path": str(series)},
        *[{"Id": ident(60 + n), "Type": "Episode", "Name": f"Episode {n}",
           "SeriesId": ident(60), "SeriesName": "Northwind", "ParentIndexNumber": 1,
           "IndexNumber": n, "Path": str(series / f"Northwind S01E0{n}.mkv")}
          for n in (1, 2, 4)],
        {"Id": ident(70), "Type": "Episode", "Name": "The Harbour",
         "SeriesId": ident(60), "SeriesName": "Northwind", "ParentIndexNumber": 2,
         "IndexNumber": 1, "Path": None},
    ]
    virtual = [{"Id": ident(71), "Type": "Episode", "Name": "Later",
                "SeriesId": ident(60), "SeriesName": "Northwind",
                "ParentIndexNumber": 2, "IndexNumber": 3, "Path": None,
                "LocationType": "Virtual"}]
    catalogue = Catalogue.from_rows(rows)
    found = scan([root], catalogue)
    kinds: dict[str, list[Any]] = {}
    for row in find_missing(catalogue, found, virtual=virtual):
        kinds.setdefault(row.kind, []).append(row)

    assert {m.name for m in kinds["no-file"]} == {"The Harbour", "Later"}
    assert any("virtual" in m.detail for m in kinds["no-file"])
    assert [m.name for m in kinds["file-gone"]] == ["Hollow Lantern"]
    gaps = {(m.series, m.season, m.episode) for m in kinds["season-gap"]}
    assert gaps == {("Northwind", 1, 3), ("Northwind", 2, 2)}
    lost = [m.path for m in kinds["release-without-item"]]
    assert str(library["lost"]) in lost and str(library["dead"]) not in lost
    text = render(find_missing(catalogue, found, virtual=virtual), "table")
    assert "season-gap" in text and "Northwind S01: no row for episode 3" in text
    many = [find_missing(Catalogue.from_rows([
        {"Id": ident(80 + n), "Type": "Episode", "Name": "x", "SeriesId": ident(79),
         "SeriesName": "Signal Hill", "ParentIndexNumber": 1, "IndexNumber": n,
         "Path": None} for n in (1, 5, 9)]))]
    assert "Signal Hill S01: no row for episode 2-4, 6-8" in render(many[0], "table")


def test_a_file_in_an_excluded_folder_is_never_called_gone(tmp_path: Path) -> None:
    root = tmp_path / "Films"
    write(root / "Winter Tide (2011).avi", b"x")
    hidden = root / "Excluded" / "Blue Canyon (1998).mkv"
    other = root / "Not Yet Made" / "Golden Meridian (1987).mkv"
    write(hidden, b"there, and not looked at")
    catalogue = Catalogue.from_rows([
        movie_row(2, "Blue Canyon", hidden, 1998),
        movie_row(5, "Golden Meridian", root / "Excluded Too" / "x.mkv", 1987),
        movie_row(4, "Golden Meridian", other, 1987),
    ])
    found = scan([root], catalogue,
                 exclude_paths=[root / "Excluded", root / "Excluded Too"])
    gone = [m.path for m in find_missing(catalogue, found) if m.kind == "file-gone"]
    assert gone == [str(other)], "only what was looked for and not found is gone"


# ---------------------------------------------------------------- the verbs
def _config(tmp_path: Path, url: str, monkeypatch: pytest.MonkeyPatch,
            parked: Path | None = None) -> Path:
    config = tmp_path / "mediatoolkit.toml"
    text = (f'[server]\nurl = "{url}"\ntoken_env = "{CREDENTIAL_VARIABLE}"\n'
            f'user_id = "{USER_ID}"\n')
    if parked is not None:
        text += f'[paths]\nparked = "{parked.as_posix()}"\n'
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv(CREDENTIAL_VARIABLE, FIXTURE_CREDENTIAL)
    return config


def test_the_verbs_sweep_apply_resume_and_report(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    url, recorder = server
    recorder.items[:] = library["rows"]
    collections = write(tmp_path / "server-data" / "collections" / "Set" / "collection.xml")
    recorder.virtual_folders = [
        {"Name": "Films", "CollectionType": "movies", "Locations": [str(library["root"])]},
        {"Name": "Collections", "CollectionType": "boxsets",
         "Locations": [str(collections.parent.parent)]},
    ]
    parked = tmp_path / "parked"
    config = _config(tmp_path, url, monkeypatch, parked)

    def run(*argv: str) -> int:
        return jfkit_cli.main(["--config", str(config), *argv])

    plan_file, out = tmp_path / "plan.json", tmp_path / "out"
    assert run("leftovers", "sweep", "--no-gate", "--release", RELEASE_JUNK,
               "--plan-out", str(plan_file), "--out", str(out)) == 0
    captured = capsys.readouterr()
    text = captured.out
    assert "dry run: nothing was moved" in text and "UNSURE" in text
    assert "walked 1 folder(s)" in text and "server's own folder" in captured.err
    assert (library["live"] / "EZTV.txt").is_file()
    document = json.loads((out / "leftovers.json").read_text(encoding="utf-8"))
    assert {c["state"] for c in document["candidates"]} >= {"park", "not released"}

    assert run("leftovers", "sweep", "--plan", str(plan_file), "--apply") == 2
    audit = tmp_path / "audit.jsonl"
    assert run("leftovers", "sweep", "--plan", str(plan_file), "--apply",
               "--audit", str(audit)) == 0
    assert not (library["live"] / "EZTV.txt").exists()
    assert run("leftovers", "sweep", "--plan", str(plan_file), "--apply",
               "--audit", str(audit)) == 0, "a second run resumes and finds it done"
    assert "skipped" in {e["event"] for e in read_audit(audit)}

    capsys.readouterr()
    assert run("leftovers", "missing", "--no-gate", "--format", "tsv") == 0
    report = capsys.readouterr().out
    assert "release-without-item" in report


def test_the_parking_directory_may_not_be_inside_a_library(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, recorder = server
    recorder.items[:] = library["rows"]
    config = _config(tmp_path, url, monkeypatch)
    assert jfkit_cli.main(["--config", str(config), "leftovers", "sweep", "--no-gate",
                           str(library["root"]), "--parked",
                           str(library["root"] / "parked")]) == 2


def test_a_held_device_is_not_walked(
    server: tuple[str, Recorder], library: dict[str, Any], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from jfkit.jobs import Gate
    from jfkit.leftovers import cli as leftovers_cli

    url, recorder = server
    recorder.items[:] = library["rows"]
    config = _config(tmp_path, url, monkeypatch)
    monkeypatch.setattr(leftovers_cli, "device_gate",
                        lambda device, **_: Gate(device, ("somebody is reading it",)))
    assert jfkit_cli.main(["--config", str(config), "leftovers", "missing",
                           str(library["root"])]) == 3
    out = capsys.readouterr().out
    assert "HELD" in out and "walked 0 folder(s)" in out


def test_a_video_parked_before_its_sidecars_resumes_with_them(
    server: tuple[str, Recorder], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step stopped after the video moved: the resumed run takes the rest."""
    from mkvkit.integrity import IntegrityReport

    from tests.test_safedelete_leftovers import failed

    url, recorder = server
    folder = tmp_path / "Series" / "Harbour Lights" / "Season 02"
    video = write(folder / "Harbour Lights - S02E05.mkv", b"\0" * 64)
    nfo = write(folder / "Harbour Lights - S02E05.nfo", "<episodedetails/>")
    thumb = write(folder / "Harbour Lights - S02E05-thumb.jpg")
    recorder.items[:] = []
    recorder.virtual_folders = [{"Name": "Series", "Locations": [str(tmp_path / "Series")]}]
    client = client_for(url, dry_run=False)
    catalogue = Catalogue.fetch(client)
    finding = Finding(CORRUPT, video, False, "failed", 64)
    report: IntegrityReport = failed(video)
    assessed = assess(client, [finding], released=[CORRUPT],
                      catalogue=catalogue, integrity={path_key(video): report})
    plan = build_plan(assessed, parked=tmp_path / "parked")
    assert [s.action for s in plan.steps] == ["leftovers.park", "leftovers.notify"]
    assert plan.steps[0].params["integrity"]["problems"], "the evidence is in the step"

    real = plan_module._park
    calls = {"n": 0}

    def stop_after_the_video(src: Path, dst: Path) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("held open")
        return real(src, dst)

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(plan_module, "_park", stop_after_the_video)
    assert not apply(plan, actions(client), audit=audit).ok
    assert not video.exists() and (nfo.exists() or thumb.exists())
    monkeypatch.setattr(plan_module, "_park", real)
    assert apply(plan, actions(client), audit=audit).ok
    assert not nfo.exists() and not thumb.exists()
    assert park_target(tmp_path / "parked", thumb).is_file()
    started = [e for e in read_audit(audit) if e["event"] == "start"]
    assert "all zeros" in json.dumps(started[0]["params"]["integrity"])
