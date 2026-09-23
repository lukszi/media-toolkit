"""The sampling worker, with neither a media file nor a model.

Everything that touches audio is behind two small interfaces, so the parts
most likely to be subtly wrong -- the window arithmetic and the bookkeeping --
are tested directly and in milliseconds. The one test that does want a real
model is marked and skips itself.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import array
import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from mkvkit.langid import worker as worker_module
from mkvkit.langid.worker import (
    DEFAULT_OFFSETS,
    MIN_USABLE_S,
    SAMPLE_RATE,
    FfmpegExtractor,
    ResultLog,
    TrackEvidence,
    Window,
    WindowResult,
    max_windows_for,
    scan_track,
    window_starts,
)


# ------------------------------------------------------------ window placement
def test_windows_avoid_the_opening_titles() -> None:
    """A window over the distributor sting reports the language of a logo."""
    starts = window_starts(3600.0, 5, 20.0)
    assert min(starts) >= 90.0
    assert starts == sorted(starts)


def test_windows_stay_inside_the_item() -> None:
    starts = window_starts(600.0, 5, 20.0)
    assert max(starts) + 20.0 <= 600.0


def test_a_short_item_does_not_lose_every_window_to_the_opening_rule() -> None:
    starts = window_starts(120.0, 3, 20.0)
    assert len(starts) == 3
    assert min(starts) > 0.0


def test_an_unknown_runtime_still_produces_windows() -> None:
    """A probe that cannot report a duration must not stop the pass."""
    assert len(window_starts(None, 5, 20.0)) == 5
    assert len(window_starts(0.0, 5, 20.0)) == 5


def test_the_retry_offsets_do_not_repeat_the_first_ones() -> None:
    from mkvkit.langid.worker import RETRY_OFFSETS

    assert not set(DEFAULT_OFFSETS) & set(RETRY_OFFSETS[:4])


def test_how_many_windows_an_item_could_possibly_supply() -> None:
    """The settle rules need this: five windows from a minute is impossible."""
    assert max_windows_for(3600.0, 20.0) == (3600 - 90) // 20
    assert max_windows_for(60.0, 20.0) == 3
    assert max_windows_for(None, 20.0) == 0


# ------------------------------------------------------------------- scanning
class FakeExtractor:
    """Returns silence of the right length, and records what was asked for."""

    def __init__(self, seconds: float = 20.0, count: int | None = None) -> None:
        self.seconds = seconds
        self.count = count
        self.asked: list[tuple[str, int, tuple[float, ...]]] = []

    def windows(
        self, path: object, audio_ord: int, starts: Sequence[float], window_s: float
    ) -> tuple[list[Window], str | None]:
        self.asked.append((str(path), audio_ord, tuple(starts)))
        wanted = starts if self.count is None else starts[: self.count]
        samples = array.array("h", [0] * int(self.seconds * SAMPLE_RATE))
        return [Window(start_s=start, pcm=samples) for start in wanted], None


class FakeDetector:
    name = "fixture"
    family = "fixture"

    def __init__(self, vector: dict[str, float] | None = None) -> None:
        self.vector = vector or {"en": 0.97, "de": 0.03}
        self.calls = 0

    def detect(self, pcm: array.array, sample_rate: int) -> dict[str, float]:
        self.calls += 1
        return dict(self.vector)


class FakeSpeech:
    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds

    def speech_seconds(self, pcm: array.array, sample_rate: int) -> float | None:
        return self.seconds


def test_a_scan_records_one_result_per_window() -> None:
    extractor, detector = FakeExtractor(), FakeDetector()
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, detector,
        extractor=extractor, runtime_s=3600.0, existing_tag="eng",
    )
    assert len(evidence.windows) == 5
    assert detector.calls == 5
    assert evidence.existing_tag == "eng"
    assert evidence.error is None


def test_detector_codes_are_canonicalised_on_the_way_in() -> None:
    """Two-letter codes from a model, three-letter codes in the file.

    Comparing them raw is how a track tagged ``deu`` and a detection of ``de``
    become a disagreement that is not one -- and a disagreement here proposes
    a write.
    """
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, FakeDetector({"en": 0.9, "de": 0.1}),
        extractor=FakeExtractor(), runtime_s=3600.0,
    )
    assert set(evidence.windows[0].probabilities) == {"eng", "deu"}


def test_codes_that_collapse_together_are_added_not_replaced() -> None:
    """A detector reporting a language twice is reporting it twice."""
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0,
        FakeDetector({"nb": 0.4, "nn": 0.3, "en": 0.3}),
        extractor=FakeExtractor(), runtime_s=3600.0,
    )
    assert evidence.windows[0].probabilities["nor"] == pytest.approx(0.7)


def test_a_window_without_speech_is_kept_but_not_counted() -> None:
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, FakeDetector(),
        extractor=FakeExtractor(), speech=FakeSpeech(1.0), runtime_s=3600.0,
        min_speech_s=3.0,
    )
    assert len(evidence.windows) == 5
    assert evidence.counted == ()
    assert evidence.no_speech


def test_a_detector_that_cannot_tell_gets_the_benefit_of_the_doubt() -> None:
    """``None`` means "no opinion", and no opinion must not silence a window."""
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, FakeDetector(),
        extractor=FakeExtractor(), speech=FakeSpeech(None), runtime_s=3600.0,
    )
    assert len(evidence.counted) == 5


def test_a_window_too_short_to_ask_about_is_dropped() -> None:
    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, FakeDetector(),
        extractor=FakeExtractor(seconds=MIN_USABLE_S / 2), runtime_s=3600.0,
    )
    assert evidence.windows == ()
    assert evidence.error == "no usable audio"


def test_a_read_that_fails_is_recorded_and_does_not_end_the_run() -> None:
    class Broken:
        def windows(self, *_args: object, **_kwargs: object) -> tuple[list[Window], None]:
            raise OSError("the device is not ready")

    evidence = scan_track(
        "/srv/media/series/Northwind/episode", 1, 0, FakeDetector(),
        extractor=Broken(), runtime_s=3600.0,
    )
    assert evidence.windows == ()
    assert evidence.error is not None
    assert "not ready" in evidence.error


# ------------------------------------------------------------------ result log
def _evidence(path: str, index: int = 1) -> TrackEvidence:
    return TrackEvidence(
        path=path, stream_index=index, model="fixture",
        windows=(WindowResult(start_s=90.0, probabilities={"eng": 0.9, "deu": 0.1},
                              speech_s=15.0),),
        existing_tag="eng", runtime_s=1800.0,
    )


def test_the_log_is_append_only_and_resumable(tmp_path: Path) -> None:
    log = ResultLog(tmp_path / "results.jsonl")
    log.append(_evidence("/srv/media/series/Northwind/a"))
    log.append(_evidence("/srv/media/series/Northwind/b"))
    assert log.done(("/srv/media/series/Northwind/a", 1, "fixture"))

    reopened = ResultLog(tmp_path / "results.jsonl")
    remaining = reopened.remaining([
        ("/srv/media/series/Northwind/a", 1, "fixture"),
        ("/srv/media/series/Northwind/c", 1, "fixture"),
    ])
    assert remaining == [("/srv/media/series/Northwind/c", 1, "fixture")]


def test_a_record_round_trips_through_the_log(tmp_path: Path) -> None:
    original = _evidence("/srv/media/series/Northwind/a")
    log = ResultLog(tmp_path / "results.jsonl")
    log.append(original)
    read_back = next(iter(ResultLog(tmp_path / "results.jsonl").read()))
    assert read_back.path == original.path
    assert read_back.existing_tag == original.existing_tag
    assert read_back.windows[0].probabilities == original.windows[0].probabilities


def test_a_truncated_last_line_does_not_lose_the_hours_before_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written line is how one of these files normally ends."""
    path = tmp_path / "results.jsonl"
    log = ResultLog(path)
    log.append(_evidence("/srv/media/series/Northwind/a"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"path": "/srv/media/series/Northwind/b", "stream_i')
    assert len(list(ResultLog(path).read())) == 1


def test_a_later_scan_supersedes_an_earlier_one(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    log = ResultLog(path)
    log.append(_evidence("/srv/media/series/Northwind/a"))
    second = TrackEvidence(
        path="/srv/media/series/Northwind/a", stream_index=1, model="fixture",
        windows=(WindowResult(start_s=90.0, probabilities={"deu": 0.95},
                              speech_s=15.0),),
    )
    log.append(second)
    latest = ResultLog(path).latest()
    assert latest[("/srv/media/series/Northwind/a", 1, "fixture")].windows[0].winner == "deu"


def test_the_model_is_part_of_the_key(tmp_path: Path) -> None:
    """Re-running with a different model adds evidence; it does not replace it."""
    path = tmp_path / "results.jsonl"
    log = ResultLog(path)
    log.append(_evidence("/srv/media/series/Northwind/a"))
    assert not log.done(("/srv/media/series/Northwind/a", 1, "another-model"))


# ------------------------------------------------------- the command line built
def test_the_duration_option_binds_to_the_output_it_caps() -> None:
    """Placed before the first output it caps that output and nothing else.

    Every later output then decodes to the end of the file, which is silent,
    correct and ruinously slow.
    """
    extractor = FfmpegExtractor()
    command = extractor._seek_command("/srv/media/series/Northwind/a", 0, 300.0, 20.0)
    assert command.index("-t") > command.index("-map")
    assert command[command.index("-t") + 1] == "20"
    assert command[-1] == "pipe:1"


def test_the_sequential_command_has_no_duration_at_all() -> None:
    command = FfmpegExtractor()._whole_command("/srv/media/series/Northwind/a", 0)
    assert "-t" not in command
    assert "-ss" not in command


def test_a_slow_first_seek_switches_to_one_sequential_decode(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unindexed container makes every seek a full read of the file."""
    calls: list[list[str]] = []
    clock = [0.0]

    def runner(command: object, _timeout: float) -> bytes:
        calls.append(list(command))  # type: ignore[arg-type]
        if "-ss" in calls[-1]:
            clock[0] += 60.0  # the first seek takes a minute: no usable index
            return b"\x00\x00" * (SAMPLE_RATE * 20)
        return b"\x00\x00" * (SAMPLE_RATE * 700)

    monkeypatch.setattr(worker_module.time, "monotonic", lambda: clock[0])
    extractor = FfmpegExtractor(runner=runner, slow_seek_s=25.0)
    windows, note = extractor.windows(
        "/srv/media/series/Northwind/a", 0, [90.0, 300.0, 600.0], 20.0
    )

    assert note is not None and "sequential" in note
    assert len(calls) == 2  # one seek, then one whole-file decode
    assert len(windows) == 3


# ------------------------------------------------------------------- the model
@pytest.mark.needs_asr
def test_a_real_detector_returns_a_probability_per_language() -> None:
    """The contract, against the real library -- and against no network.

    The model is only loaded from whatever cache is already on the machine: a
    test that downloads a gigabyte is a test that does not run.
    """
    from mkvkit.langid.worker import WhisperDetector

    try:
        detector = WhisperDetector(
            "tiny", device="cpu", compute_type="int8", local_files_only=True
        )
    except Exception as exc:
        pytest.skip(f"no locally cached model: {exc}")
    silence = array.array("h", [0] * (SAMPLE_RATE * 5))
    vector = detector.detect(silence, SAMPLE_RATE)
    assert vector
    assert all(0.0 <= probability <= 1.0 for probability in vector.values())
    assert sum(vector.values()) == pytest.approx(1.0, abs=0.05)


def test_a_record_is_json_and_stays_json() -> None:
    record = _evidence("/srv/media/series/Northwind/a").to_record()
    assert json.loads(json.dumps(record))["windows"][0]["top"][0] == ["eng", 0.9]
