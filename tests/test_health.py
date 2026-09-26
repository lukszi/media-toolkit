"""The library health scan: a cheap sweep of every file, a full read of the suspects.

The files these tests exist for keep their headers: a download that reserved
its space and never filled it, and a copy that stopped half way. The fixtures
make exactly those, from a real playable file -- zeros over everything but
the first and last few kilobytes, and only the first three fifths of the
bytes -- next to an untouched copy that has to come out OK.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mkvkit import cli as mkvkit_cli
from mkvkit.health import (
    CORRUPT,
    OK,
    SUSPECT,
    UNREADABLE,
    FileResult,
    Progress,
    Settings,
    StateFile,
    Target,
    confirm,
    declared_end,
    manifest_rows,
    plan,
    probe_findings,
    render_table,
    sweep,
    sweep_one,
    tsv_text,
)
from mkvkit.integrity import IntegrityReport, ZeroSample
from mkvkit.run import default_runner

from tests.fake_server import client_for
from tests.fake_server import fake_server as running_server

#: small blocks, because the fixtures are small
SMALL = Settings(blocks=16, block_size=16 << 10)


def zero_most_of(source: Path, target: Path, *, keep_head: int = 32 << 10,
                 keep_tail: int = 16 << 10) -> Path:
    """A copy whose header and index survive and whose body is zeros."""
    data = bytearray(source.read_bytes())
    end = len(data) - keep_tail
    data[keep_head:end] = bytes(end - keep_head)
    target.write_bytes(bytes(data))
    return target


def cut_off(source: Path, target: Path, share: float = 0.6) -> Path:
    """A copy that stopped part of the way through."""
    data = source.read_bytes()
    target.write_bytes(data[:int(len(data) * share)])
    return target


def target_of(path: Path, device: str = "lane-a") -> Target:
    stat = path.stat()
    return Target(path, stat.st_size, stat.st_mtime_ns, device)


@pytest.fixture
def library(media_fixtures: dict[str, Path], tmp_path: Path) -> dict[str, Path]:
    """One healthy episode, one never filled, one cut off, and a cut-off film."""
    root = tmp_path / "library"
    (root / "Harbour Lights").mkdir(parents=True)
    (root / "Blue Canyon (1998)").mkdir()
    mkv = media_fixtures["tiny_multitrack.mkv"]
    mp4 = media_fixtures["tiny_multitrack.mp4"]
    healthy = root / "Harbour Lights" / "Harbour Lights - S01E01.mkv"
    shutil.copyfile(mkv, healthy)
    film = root / "Blue Canyon (1998)" / "Blue Canyon (1998).mp4"
    shutil.copyfile(mp4, film)
    return {
        "root": root,
        "healthy": healthy,
        "film": film,
        "zeroed": zero_most_of(mkv, root / "Harbour Lights" / "Harbour Lights - S01E02.mkv"),
        "truncated": cut_off(mkv, root / "Harbour Lights" / "Harbour Lights - S01E03.mkv"),
        "truncated_mp4": cut_off(
            mp4, root / "Blue Canyon (1998)" / "Blue Canyon (1998)-trailer.mp4", share=0.5
        ),
    }


# ------------------------------------------------------- the container extent
@pytest.mark.needs_ffmpeg
def test_a_whole_file_ends_where_its_container_says(media_fixtures: dict[str, Path]) -> None:
    for name in ("tiny_multitrack.mkv", "tiny_multitrack.mp4"):
        path = media_fixtures[name]
        assert declared_end(path) == path.stat().st_size, name


@pytest.mark.needs_ffmpeg
def test_a_cut_off_file_is_shorter_than_its_container_says(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    for name in ("tiny_multitrack.mkv", "tiny_multitrack.mp4"):
        whole = media_fixtures[name]
        short = cut_off(whole, tmp_path / name)
        end = declared_end(short)
        assert end is not None and end > short.stat().st_size, name


def test_bytes_that_are_no_known_container_declare_nothing(tmp_path: Path) -> None:
    path = tmp_path / "noise.mkv"
    path.write_bytes(os.urandom(4096))
    assert declared_end(path) is None


def test_an_avi_declares_the_end_of_its_last_chunk(tmp_path: Path) -> None:
    body = b"AVI " + b"LIST" + (100).to_bytes(4, "little") + bytes(100)
    riff = b"RIFF" + len(body).to_bytes(4, "little") + body
    path = tmp_path / "Signal Hill - S01E01.avi"
    path.write_bytes(riff)
    assert declared_end(path) == len(riff)
    path.write_bytes(riff[:60])
    assert (declared_end(path) or 0) > 60


# -------------------------------------------------------- the probe's answers
def probed(container: float, *streams: dict[str, Any]) -> dict[str, Any]:
    return {"format": {"duration": str(container)}, "streams": list(streams)}


def test_a_track_that_states_twice_the_container_is_a_suspect() -> None:
    """The shape of the real case: one stray packet stamped far past the end."""
    found, duration = probe_findings(probed(
        1270.4,
        {"index": 0, "codec_type": "video", "height": 1080,
         "tags": {"DURATION": "00:42:22.000000000"}},
        {"index": 1, "codec_type": "audio", "tags": {"DURATION-eng": "00:20:55.000000000"}},
    ), 650 << 20, Settings())
    assert duration == pytest.approx(1270.4)
    assert any("stream 0 (video) states 2542.0 s" in f for f in found)
    assert not any("stream 1" in f for f in found), "within tolerance"


def test_a_file_much_smaller_than_its_tracks_count_is_a_suspect() -> None:
    found, _ = probe_findings(probed(
        2400.0,
        {"index": 0, "codec_type": "video", "height": 720,
         "tags": {"NUMBER_OF_BYTES": str(900 << 20)}},
        {"index": 1, "codec_type": "audio", "tags": {"NUMBER_OF_BYTES-eng": str(100 << 20)}},
    ), 400 << 20, Settings())
    assert any("holds 40%" in f and "statistics count" in f for f in found)


def test_a_file_the_size_its_bitrates_imply_passes() -> None:
    found, _ = probe_findings(probed(
        100.0,
        {"index": 0, "codec_type": "video", "height": 1080, "bit_rate": "4000000"},
        {"index": 1, "codec_type": "audio", "bit_rate": "192000"},
    ), int(4_192_000 * 100 / 8), Settings())
    assert found == []


def test_implausibly_little_for_the_picture_size_is_a_suspect() -> None:
    found, _ = probe_findings(probed(
        2700.0, {"index": 0, "codec_type": "video", "height": 1080},
    ), 20 << 20, Settings())
    assert any("implausibly little for 1080-line video" in f for f in found)


def test_no_duration_and_no_tracks_are_both_suspects() -> None:
    assert probe_findings({"format": {}, "streams": [{"codec_type": "video"}]},
                          1000, Settings())[0] == ["the container states no duration"]
    assert probe_findings(probed(10.0), 1000, Settings())[0] == [
        "the probe found no audio or video track"
    ]


# --------------------------------------------------------------------- stage 1
@pytest.mark.needs_ffmpeg
def test_stage_one_calls_the_whole_file_ok_and_the_broken_ones_suspect(
    library: dict[str, Path]
) -> None:
    runner = default_runner()
    healthy = sweep_one(target_of(library["healthy"]), SMALL, runner=runner)
    assert healthy.verdict == OK, healthy.evidence
    assert healthy.sampled_blocks == 16 and healthy.zero_blocks == 0

    zeroed = sweep_one(target_of(library["zeroed"]), SMALL, runner=runner)
    assert zeroed.verdict == SUSPECT
    assert any("nothing but zero bytes" in e for e in zeroed.evidence)

    for name in ("truncated", "truncated_mp4"):
        found = sweep_one(target_of(library[name]), SMALL, runner=runner)
        assert found.verdict == SUSPECT, name
        assert any("cut off" in e for e in found.evidence), (name, found.evidence)


def test_a_file_that_cannot_be_opened_is_unreadable(tmp_path: Path) -> None:
    gone = Target(tmp_path / "Northwind - S01E03.mkv", 10, 0, "lane-a")
    found = sweep_one(gone, SMALL, runner=default_runner())
    assert found.verdict == UNREADABLE
    assert "cannot be read" in found.evidence[0]


# --------------------------------------------------------------------- stage 2
@pytest.mark.needs_ffmpeg
def test_stage_two_confirms_the_broken_files_and_clears_a_healthy_one(
    library: dict[str, Path], tmp_path: Path
) -> None:
    runner = default_runner()
    suspects = [
        sweep_one(target_of(library[name]), SMALL, runner=runner)
        for name in ("zeroed", "truncated", "truncated_mp4")
    ]
    # a healthy file suspected for some other reason is cleared by the full read
    wrongly = sweep_one(target_of(library["healthy"]), SMALL, runner=runner)
    wrongly = FileResult(**{**wrongly.as_dict(), "verdict": SUSPECT,
                            "evidence": ("a made-up doubt",), "notes": ()})
    state = StateFile(tmp_path / "state.jsonl")
    confirmed, missed = confirm([*suspects, wrongly], settings=SMALL, state=state)
    state.close()
    assert missed == []
    verdicts = {Path(r.path).name: r.verdict for r in confirmed}
    assert verdicts == {
        library["zeroed"].name: CORRUPT,
        library["truncated"].name: CORRUPT,
        library["truncated_mp4"].name: CORRUPT,
        library["healthy"].name: OK,
    }
    assert all(r.stage == 2 for r in confirmed)
    assert all(any(e.startswith("stage 1:") for e in r.evidence) for r in confirmed)
    assert len(state.load()) == 4, "every confirmation is saved"


def test_a_file_nothing_could_open_is_unreadable_not_corrupt(tmp_path: Path) -> None:
    suspect = FileResult(path=str(tmp_path / "gone.mkv"), size=10, mtime_ns=1,
                         device="lane-a", verdict=SUSPECT, evidence=("doubt",))

    def check(path: Path, **_options: Any) -> IntegrityReport:
        return IntegrityReport(path=path, evidence=False,
                               problems=("the file cannot be read: gone",))

    confirmed, _ = confirm([suspect], check=check)
    assert confirmed[0].verdict == UNREADABLE


# ------------------------------------------------------ one reader per device
class Meter:
    """How many reads are in flight, overall and per device."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now: dict[str, int] = defaultdict(int)
        self.peak: dict[str, int] = defaultdict(int)

    def reading(self, device: str, seconds: float = 0.02) -> None:
        with self.lock:
            self.now[device] += 1
            self.now["*"] += 1
            for key in (device, "*"):
                self.peak[key] = max(self.peak[key], self.now[key])
        time.sleep(seconds)
        with self.lock:
            self.now[device] -= 1
            self.now["*"] -= 1


def two_disks(tmp_path: Path, per_disk: int = 6) -> tuple[list[Path], Callable[[Path | str], str]]:
    """Files on two stand-in disks: the first folder under the root names the disk."""
    files: list[Path] = []
    for disk in ("disk-one", "disk-two"):
        for n in range(per_disk):
            path = tmp_path / disk / "Northwind" / f"Northwind - S01E{n + 1:02d}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(2048))
            files.append(path)

    def device_of(path: Path | str) -> str:
        return Path(path).relative_to(tmp_path).parts[0]

    return files, device_of


def test_no_disk_ever_has_two_readers_and_the_disks_run_side_by_side(tmp_path: Path) -> None:
    files, device_of = two_disks(tmp_path)
    meter = Meter()

    def check(target: Target) -> FileResult:
        meter.reading(target.device)
        return FileResult(str(target.path), target.size, target.mtime_ns, target.device, OK)

    targets, _known, _skipped, _population = plan(
        [tmp_path], state={}, settings=SMALL, device_of=device_of,
    )
    assert len(targets) == len(files)
    results, missed = sweep(targets, SMALL, check=check, device_of=device_of)
    assert missed == [] and len(results) == len(files)
    assert meter.peak["disk-one"] == 1 and meter.peak["disk-two"] == 1
    assert meter.peak["*"] == 2, "the two disks were read at the same time"


def test_stage_two_keeps_to_one_reader_per_disk_too(tmp_path: Path) -> None:
    files, device_of = two_disks(tmp_path, per_disk=4)
    meter = Meter()
    suspects = [
        FileResult(str(p), p.stat().st_size, p.stat().st_mtime_ns, device_of(p), SUSPECT)
        for p in files
    ]

    def check(path: Path, **_options: Any) -> IntegrityReport:
        meter.reading(device_of(path))
        return IntegrityReport(path=Path(path), size=2048,
                               zero=ZeroSample(size=2048, block_size=2048))

    confirmed, _ = confirm(suspects, check=check, device_of=device_of)
    assert {r.verdict for r in confirmed} == {OK}
    assert meter.peak["disk-one"] == 1 and meter.peak["disk-two"] == 1
    assert meter.peak["*"] == 2


def test_the_gate_is_asked_before_every_file_and_a_refusal_is_not_recorded(
    tmp_path: Path
) -> None:
    files, device_of = two_disks(tmp_path, per_disk=2)
    asked: list[tuple[str, str]] = []

    def gate(device: str, target: Target) -> None:
        asked.append((device, target.path.name))
        if device == "disk-two":
            raise TimeoutError("disk-two stayed RED (playback)")

    state = StateFile(tmp_path / "state.jsonl")
    targets, *_ = plan([tmp_path], state={}, settings=SMALL, device_of=device_of)
    results, missed = sweep(
        targets, SMALL, state=state, device_of=device_of, before_each=gate,
        check=lambda t: FileResult(str(t.path), t.size, t.mtime_ns, t.device, OK),
    )
    state.close()
    assert len(asked) == len(files)
    assert {r.device for r in results} == {"disk-one"}
    assert len(missed) == 2 and all("stayed RED" in m for m in missed)
    saved = {device_of(r.path) for r in state.load().values()}
    assert saved == {"disk-one"}, "a file the gate held is not answered for"


# ---------------------------------------------------- incremental and resumed
def test_a_second_run_reads_only_what_changed(tmp_path: Path) -> None:
    files, device_of = two_disks(tmp_path, per_disk=3)
    state = StateFile(tmp_path / "state.jsonl")
    reads: list[str] = []

    def check(target: Target) -> FileResult:
        reads.append(target.path.name)
        return FileResult(str(target.path), target.size, target.mtime_ns, target.device, OK,
                          settings=SMALL.fingerprint())

    targets, *_ = plan([tmp_path], state=state.load(), settings=SMALL, device_of=device_of)
    sweep(targets, SMALL, state=state, check=check, device_of=device_of)
    state.close()
    assert len(reads) == 6

    again, known, _skipped, _population = plan(
        [tmp_path], state=state.load(), settings=SMALL, device_of=device_of
    )
    assert again == [] and len(known) == 6

    changed = files[0]
    changed.write_bytes(os.urandom(4096))
    again, known, *_ = plan([tmp_path], state=state.load(), settings=SMALL,
                            device_of=device_of)
    assert [t.path for t in again] == [changed]
    assert len(known) == 5

    other = Settings(blocks=32, block_size=SMALL.block_size)
    again, *_ = plan([tmp_path], state=state.load(), settings=other, device_of=device_of)
    assert len(again) == 6, "other settings are another measurement"
    again, *_ = plan([tmp_path], state=state.load(), settings=SMALL, device_of=device_of,
                     rescan=True)
    assert len(again) == 6


def test_an_interrupted_run_resumes_where_it_stopped(tmp_path: Path) -> None:
    files, device_of = two_disks(tmp_path, per_disk=4)
    state = StateFile(tmp_path / "state.jsonl")
    ticks = iter(range(10_000))
    progress = Progress(every_s=float("inf"), out=lambda _l: None,
                        clock=lambda: float(next(ticks)))

    def check(target: Target) -> FileResult:
        return FileResult(str(target.path), target.size, target.mtime_ns, target.device, OK,
                          settings=SMALL.fingerprint())

    targets, *_ = plan([tmp_path], state={}, settings=SMALL, device_of=device_of)
    # the budget runs out part of the way through: the clock moves on every look
    results, missed = sweep(targets, SMALL, state=state, check=check, progress=progress,
                            deadline=6.0, device_of=device_of)
    state.close()
    assert 0 < len(results) < len(files)
    assert len(missed) == len(files) - len(results)
    assert all("time budget" in m for m in missed)
    # a torn last line, as a machine that lost power leaves it
    with (tmp_path / "state.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"schema": 1, "path": "half')

    rest, known, *_ = plan([tmp_path], state=state.load(), settings=SMALL,
                           device_of=device_of)
    assert len(known) == len(results)
    assert sorted(t.path for t in rest) == sorted(
        p for p in files if str(p) not in {r.path for r in results}
    )


def test_compaction_keeps_one_line_per_file(tmp_path: Path) -> None:
    state = StateFile(tmp_path / "state.jsonl")
    for verdict in (SUSPECT, CORRUPT):
        state.append(FileResult("/srv/media/a.mkv", 1, 1, "/srv", verdict))
    state.append(FileResult("/srv/media/b.mkv", 1, 1, "/srv", OK))
    state.compact()
    lines = (tmp_path / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert state.load()[StateFile.key("/srv/media/a.mkv")].verdict == CORRUPT


def test_a_subset_is_spread_over_each_disk(tmp_path: Path) -> None:
    _files, device_of = two_disks(tmp_path, per_disk=6)
    targets, _known, _skipped, population = plan(
        [tmp_path], state={}, settings=SMALL, device_of=device_of, subset=2,
    )
    assert population == {"disk-one": 6, "disk-two": 6}
    names = sorted((t.device, t.path.name) for t in targets)
    assert [n for d, n in names if d == "disk-one"] == [
        "Northwind - S01E01.mkv", "Northwind - S01E04.mkv"
    ]


def test_progress_reports_rate_and_eta_per_disk() -> None:
    now = [0.0]
    lines: list[str] = []
    progress = Progress(every_s=5.0, out=lines.append, clock=lambda: now[0])
    targets = [Target(Path(f"/srv/media/{n}.mkv"), 1 << 20, 0, "/srv") for n in range(4)]
    progress.plan(targets, population={"/srv": 40})
    progress.start("/srv")
    for _ in range(2):
        now[0] += 5.0
        progress.done("/srv", size=1 << 20, read=1 << 20, seconds=5.0)
    stats = progress.summary()[0]
    assert stats["scanned"] == 2 and stats["files_per_s"] == pytest.approx(0.2)
    assert stats["projected_s"] == pytest.approx(200.0), "forty files at this pace"
    assert any("ETA 0:00:10" in line for line in lines)


# --------------------------------------------------------------------- output
def test_the_manifest_lists_only_confirmed_corrupt_files() -> None:
    rows = manifest_rows([
        FileResult("/srv/media/a.mkv", 1, 1, "/srv", CORRUPT, stage=2,
                   evidence=("stage 1: zeros", "stage 2: packets for 2 s"),
                   item_id="00000000-0000-0000-0000-000000000001"),
        FileResult("/srv/media/b.mkv", 1, 1, "/srv", SUSPECT, evidence=("zeros",)),
        FileResult("/srv/media/c.mkv", 1, 1, "/srv", OK),
    ])
    assert rows == [{
        "item_id": "00000000-0000-0000-0000-000000000001", "path": "/srv/media/a.mkv",
        "category": "corrupt", "reason": "stage 2: packets for 2 s",
    }]


def test_the_table_puts_the_worst_first_and_leaves_ok_out() -> None:
    results = [
        FileResult("/srv/media/ok.mkv", 1, 1, "/srv", OK),
        FileResult("/srv/media/doubt.mkv", 1, 1, "/srv", SUSPECT, evidence=("zeros",)),
        FileResult("/srv/media/gone.mkv", 1, 1, "/srv", CORRUPT, title="Northwind S01E03"),
    ]
    table = render_table(results)
    assert table.index("gone.mkv") < table.index("doubt.mkv")
    assert "ok.mkv" not in table and "Northwind S01E03" in table
    assert "ok.mkv" in render_table(results, include_ok=True)
    assert tsv_text(results).splitlines()[0].startswith("verdict\tstage\tdevice\tpath")


# ------------------------------------------------------------------ the verb
@pytest.mark.needs_ffmpeg
def test_the_verb_finds_the_broken_files_writes_its_reports_and_resumes(
    library: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state, report, table, manifest = (
        tmp_path / "state.jsonl", tmp_path / "health.json", tmp_path / "health.tsv",
        tmp_path / "manifest.tsv",
    )
    argv = [
        "health", str(library["root"]), "--gate", "off", "--state", str(state),
        "--blocks", "16", "--block-kib", "16", "--json", str(report), "--tsv", str(table),
        "--manifest", str(manifest), "--exclude", "*-trailer.mp4",
    ]
    assert mkvkit_cli.main(argv) == 1
    out = capsys.readouterr()
    assert "CORRUPT" in out.out and "nothing was moved" in out.out
    assert "stage 2: confirming 2 of 2 suspect(s)" in out.err
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["counts"] == {OK: 2, SUSPECT: 0, CORRUPT: 2, UNREADABLE: 0}
    assert document["devices"][0]["scanned"] == 4
    assert any("excluded" in s for s in document["skipped"])
    rows = manifest.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3 and rows[0] == "item_id\tpath\tcategory\treason"
    assert len(table.read_text(encoding="utf-8").splitlines()) == 5
    for name in ("healthy", "film", "zeroed", "truncated"):
        assert library[name].exists(), "nothing is ever moved"

    # the second run reads nothing and confirms nothing again
    assert mkvkit_cli.main(argv) == 1
    out = capsys.readouterr()
    assert "0 file(s) to read, 4 unchanged" in out.err
    assert "stage 2" not in out.err
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["counts"][CORRUPT] == 2


@pytest.mark.needs_ffmpeg
def test_a_healthy_tree_exits_zero(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "films"
    root.mkdir()
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], root / "Winter Tide (2011).mkv")
    assert mkvkit_cli.main([
        "health", str(root), "--gate", "off", "--state", str(tmp_path / "s.jsonl"),
        "--blocks", "8", "--block-kib", "16",
    ]) == 0
    assert "OK 1 SUSPECT 0 CORRUPT 0 UNREADABLE 0" in capsys.readouterr().out


# ------------------------------------------------ the scan's server half
def test_a_scan_names_each_file_that_is_not_ok_by_its_server_item() -> None:
    from jfkit.config import Config
    from jfkit.healthlink import title_of, with_titles

    with running_server() as (url, recorder):
        client = client_for(url)
        episode = recorder.items[6]
        results = [
            FileResult(episode["Path"].upper().replace("/", "\\"), 1, 1, "/srv", CORRUPT),
            FileResult(recorder.items[1]["Path"], 1, 1, "/srv", OK),
            FileResult("/srv/media/movies/nobody plays this.mkv", 1, 1, "/srv", CORRUPT),
        ]
        named = with_titles(Config(), results, client=client)
    assert named[0].item_id == episode["Id"]
    assert named[0].title == "Northwind S01E07 Northwind"
    assert named[1].item_id is None, "OK files are not looked up"
    assert named[2].item_id is None
    assert title_of({"Name": "Blue Canyon", "ProductionYear": 1998}) == "Blue Canyon (1998)"
    assert not recorder.posted


def test_the_scan_gate_is_built_from_the_configuration() -> None:
    from jfkit.config import Config
    from jfkit.healthlink import lane_gate
    from jfkit.jobs import Gate, Signal

    waits: list[tuple[str, str | None]] = []

    class Watch:
        def waiting(self, device: str, why: str | None) -> None:
            waits.append((device, why))

    built = lane_gate(Config(), server=True, every_s=5, timeout_s=60,
                      locks=["/srv/held.lock"], progress=Watch())
    assert built.client is None, "no server configured: this machine only"
    assert built.every_s == 5 and built.timeout_s == 60
    assert built.locks == ("/srv/held.lock",)
    assert built.on_hold is not None
    built.on_hold("/srv", Gate("/srv", reasons=("[lock] held",),
                               signals=(Signal("lock", "held"),)))
    assert waits == [("/srv", "RED by lock: [lock] held")]


@pytest.mark.needs_ffmpeg
def test_from_state_reads_nothing_new_and_confirms_what_the_sweep_found(
    library: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.jsonl"
    common = ["--gate", "off", "--state", str(state), "--blocks", "16", "--block-kib", "16",
              "--exclude", "*.mp4"]
    assert mkvkit_cli.main(["health", str(library["root"]), *common, "--no-confirm"]) == 1
    capsys.readouterr()
    fresh = library["root"] / "Harbour Lights" / "Harbour Lights - S01E04.mkv"
    shutil.copyfile(library["healthy"], fresh)
    report = tmp_path / "health.json"
    assert mkvkit_cli.main([
        "health", str(library["root"]), *common, "--from-state", "--confirm", "1",
        "--json", str(report),
    ]) == 1
    err = capsys.readouterr().err
    assert "1 file(s) the state file has no answer for" in err
    assert "stage 2: confirming 1 of 2 suspect(s)" in err
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["counts"] == {OK: 1, SUSPECT: 1, CORRUPT: 1, UNREADABLE: 0}
    assert fresh.name not in {Path(r["path"]).name for r in document["results"]}


# ------------------------------------------------- a video suffix on a text
def test_a_text_file_with_a_stream_suffix_is_reported_not_judged(tmp_path: Path) -> None:
    source = tmp_path / "Hollowmere" / "routes.ts"
    source.parent.mkdir()
    source.write_text("export const routes = [];\n" * 40, encoding="utf-8")
    found = sweep_one(target_of(source), SMALL, runner=default_runner())
    assert found.verdict == OK and not found.media
    assert "not a media file" in found.notes[0]


def test_a_stream_or_a_zeroed_file_with_that_suffix_is_still_judged(tmp_path: Path) -> None:
    from mkvkit.health import not_a_stream

    stream = tmp_path / "Northwind - S01E03.ts"
    stream.write_bytes(b"".join(b"\x47" + os.urandom(187) for _ in range(40)))
    assert not_a_stream(stream) is None
    zeroed = tmp_path / "Northwind - S01E04.ts"
    zeroed.write_bytes(bytes(8192))
    assert not_a_stream(zeroed) is None


def test_suspects_of_older_rules_are_read_again_and_nothing_else(tmp_path: Path) -> None:
    files, device_of = two_disks(tmp_path, per_disk=2)
    fingerprint = SMALL.fingerprint()
    state = {
        StateFile.key(p): FileResult(
            str(p), p.stat().st_size, p.stat().st_mtime_ns, device_of(p),
            SUSPECT if n == 0 else OK, settings=fingerprint, rules=1,
        )
        for n, p in enumerate(files)
    }
    again, known, *_ = plan([tmp_path], state=state, settings=SMALL, device_of=device_of)
    assert [t.path for t in again] == [files[0]]
    assert len(known) == 3
