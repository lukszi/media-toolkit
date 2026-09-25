"""The sub-commands: the shapes they take, the two states they write in, and
the exit codes they carry.

Most of this runs with nothing installed, because a command's contract -- what
it accepts, what it refuses, what it returns -- is not a property of ffmpeg.
The three that touch a file are marked and use the drifting pair.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from dubalign.cli import REGISTRY, build_parser, main
from dubalign.encode import ENCODER_DELAY_SAMPLES, ENCODINGS, encode, encode_command
from dubalign.plan import Seam, Segment, SplicePlan
from mkvkit.run import Result

EXPECTED_VERBS = {
    "probe", "decode", "map", "drift", "changepoints", "plan", "splice",
    "encode", "verify", "controls",
}


def recording_runner(calls: list[list[str]]) -> Any:
    def run(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        calls.append([tool, *(str(a) for a in args)])
        return Result(tool=tool, argv=(tool,), returncode=0, stdout="", stderr="")

    return run


# ------------------------------------------------------------------ the parser
def test_every_stage_of_the_pipeline_has_a_verb() -> None:
    assert set(REGISTRY) == EXPECTED_VERBS


def test_the_verbs_are_registered_in_the_order_a_job_uses_them() -> None:
    assert list(REGISTRY)[:5] == ["probe", "decode", "map", "drift", "changepoints"]


def test_no_verb_at_all_prints_help_and_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    assert "COMMAND" in capsys.readouterr().out


def test_the_version_is_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    from dubalign import __version__

    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out


def test_every_writing_verb_defaults_to_the_dry_run() -> None:
    """The rule for the whole toolkit: writing is asked for twice."""
    parser = build_parser()
    for verb, required in (
        ("plan", ["a.mkv", "b.mkv", "--out", "p.toml"]),
        ("splice", ["p.toml", "b.mkv", "--out", "o.f32le"]),
        ("encode", ["b.f32le", "--out", "o.flac"]),
    ):
        assert parser.parse_args([verb, *required]).apply is False, verb
        assert parser.parse_args([verb, *required, "--apply"]).apply is True, verb


def test_a_reading_verb_has_no_apply_switch_to_forget() -> None:
    parser = build_parser()
    assert not hasattr(parser.parse_args(["map", "a.mkv", "b.mkv"]), "apply")
    assert not hasattr(parser.parse_args(["verify", "a.mkv", "b.mkv"]), "apply")


def test_the_two_states_are_the_only_two() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["encode", "b.f32le", "--out", "o.flac", "--apply", "--dry-run"])


def test_every_measurement_verb_takes_the_same_pair_of_sources() -> None:
    parser = build_parser()
    for verb, extra in (
        ("map", []), ("changepoints", []), ("verify", []),
        ("drift", ["--base-ms", "120"]),
    ):
        args = parser.parse_args([verb, "a.mkv", "b.mkv", *extra])
        assert args.reference == Path("a.mkv")
        assert args.other == Path("b.mkv")
        assert args.reference_stream == 0 and args.other_stream == 0


def test_the_drift_verb_requires_the_offset_it_measures_around() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["drift", "a.mkv", "b.mkv"])


# ------------------------------------------------------------------- the plan
def a_plan() -> SplicePlan:
    return SplicePlan(
        sample_rate=48_000, channels=1, total_frames=48_000 * 30,
        seams=(Seam(t=10.0, half_width_s=0.010, step_s=-0.040),),
        segments=(
            Segment("head", "other", 5760),
            Segment("tail", "other", 3840, anchor_frame=480_000),
        ),
    )


def test_a_plan_that_fails_its_own_check_stops_the_splice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = SplicePlan(
        sample_rate=48_000, channels=1, total_frames=48_000,
        seams=(Seam(t=0.5), Seam(t=0.6)), segments=(Segment("only", "other", 0),),
    )
    path = tmp_path / "plan.toml"
    path.write_text(broken.to_toml(), encoding="utf-8")
    assert main(["splice", str(path), "x.mkv", "--out", str(tmp_path / "o.f32le")]) == 1
    assert "PROBLEM" in capsys.readouterr().out


# ----------------------------------------------------------------- the encode
def test_the_lossy_encoder_delay_is_trimmed_by_name_not_discovered_again() -> None:
    args = encode_command(
        "in.f32le", "out.ac3", ENCODINGS["ac3"], channels=6,
        trim_samples=ENCODER_DELAY_SAMPLES["ac3"],
    )
    assert "atrim=start_sample=256" in args
    assert ENCODER_DELAY_SAMPLES["ac3"] == 256


def test_the_lossless_encoder_has_no_delay_to_trim() -> None:
    written = encode(np.zeros((100, 2)), "out.flac", encoding="flac", dry_run=True)
    assert written.trimmed_samples == 0
    assert "atrim" not in " ".join(written.command)


def test_a_dry_run_builds_the_command_and_writes_nothing(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "out.ac3"
    written = encode(
        np.zeros((100, 6)), target, encoding="ac3", dry_run=True,
        runner=recording_runner(calls),
    )
    assert calls == []
    assert not target.exists()
    assert written.trimmed_samples == 256
    assert "would" not in written.describe()


def test_an_apply_runs_the_program_once_and_cleans_up_after_itself(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    target = tmp_path / "out.flac"
    encode(
        np.zeros((100, 2), dtype=np.float32), target, encoding="flac", dry_run=False,
        runner=recording_runner(calls), work_dir=tmp_path,
    )
    assert len(calls) == 1 and calls[0][0] == "ffmpeg"
    assert list(tmp_path.glob("*.f32le")) == [], "the scratch dump is not left behind"


def test_an_apply_never_deletes_the_samples_it_was_given(tmp_path: Path) -> None:
    """Built samples and output sharing a stem once meant the input was lost.

    The scratch dump was named after the output, in the same directory, so it
    overwrote ``built.f32le`` and then removed it.
    """
    samples = tmp_path / "built.f32le"
    block = np.arange(200, dtype=np.float32).reshape(-1, 2)
    block.tofile(samples)
    before = samples.read_bytes()
    calls: list[list[str]] = []
    written = encode(
        np.fromfile(samples, dtype=np.float32).reshape(-1, 2), tmp_path / "built.flac",
        encoding="flac", dry_run=False, runner=recording_runner(calls),
    )
    assert samples.exists(), "the input was deleted"
    assert samples.read_bytes() == before, "the input was overwritten"
    scratch = calls[0][calls[0].index("-i") + 1]
    assert Path(scratch) != samples
    assert not Path(scratch).exists()
    assert str(samples) not in written.command
    assert sorted(p.name for p in tmp_path.iterdir()) == ["built.f32le"]


def test_a_failed_encode_leaves_the_input_and_no_scratch(tmp_path: Path) -> None:
    samples = tmp_path / "built.f32le"
    np.zeros((50, 2), dtype=np.float32).tofile(samples)

    def failing(tool: str, args: Any, *, ok: Any = (0,)) -> Result:
        raise RuntimeError("the encoder stopped")

    with pytest.raises(RuntimeError):
        encode(np.zeros((50, 2)), tmp_path / "built.flac", dry_run=False, runner=failing)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["built.f32le"]


def test_verify_never_reuses_a_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verification that read an old dump would pass for a file it never saw."""
    import dubalign.cli as cli

    seen: list[dict[str, Any]] = []

    class Stop(Exception):
        pass

    def recording_decode(source: Any, **kwargs: Any) -> Any:
        seen.append({"source": source, **kwargs})
        if len(seen) == 2:
            raise Stop
        return None

    monkeypatch.setattr(cli, "decode", recording_decode)
    with pytest.raises(Stop):
        main(["verify", "keeper.mkv", "track.flac", "--work", str(tmp_path)])
    assert [call["reuse"] for call in seen] == [False, False]


def test_an_encoding_nobody_offers_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="flac"):
        encode(np.zeros((10, 2)), "out.wav", encoding="wav")


def test_the_metadata_goes_on_the_track_and_not_the_container() -> None:
    args = encode_command(
        "in.f32le", "out.flac", ENCODINGS["flac"], language="eng", title="Example"
    )
    assert "-metadata:s:a:0" in args
    assert "language=eng" in args and "title=Example" in args


# --------------------------------------------------------------- the controls
def test_the_controls_verb_passes_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["controls"]) == 0
    assert "4 of 4 controls passed" in capsys.readouterr().out


# ------------------------------------------------------------- against a file
@pytest.mark.needs_ffmpeg
def test_probe_reads_the_pair_and_says_what_is_in_it(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    if "drift_pair.mka" not in media_fixtures:
        pytest.skip("the drifting pair was not built")
    assert main(["probe", str(media_fixtures["drift_pair.mka"])]) == 0
    out = capsys.readouterr().out
    assert "a:0" in out and "a:1" in out


@pytest.mark.needs_ffmpeg
def test_the_whole_pipeline_runs_from_the_command_line(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every verb in the order a job uses them, on one file, ending in a pass."""
    if "drift_pair.mka" not in media_fixtures:
        pytest.skip("the drifting pair was not built")
    source = str(media_fixtures["drift_pair.mka"])
    work = ["--work", str(tmp_path / "work")]
    pair = [source, source, "--reference-stream", "0", "--other-stream", "1"]

    assert main(["map", *pair, *work, "--window", "8", "--step", "8"]) == 0
    assert main(["changepoints", *pair, *work]) == 0

    plan_path = tmp_path / "plan.toml"
    assert main(["plan", *pair, *work, "--out", str(plan_path)]) == 0
    assert not plan_path.exists(), "the dry run must not have written it"
    assert main(["plan", *pair, *work, "--out", str(plan_path), "--apply"]) == 0
    assert plan_path.exists()

    built = tmp_path / "built.f32le"
    splice_args = [
        "splice", str(plan_path), source, "--other-stream", "1", "--out", str(built), *work
    ]
    assert main(splice_args) == 0
    assert not built.exists()
    assert main([*splice_args, "--apply"]) == 0
    assert built.exists()

    capsys.readouterr()
    written = tmp_path / "out.flac"
    assert main(["encode", str(built), "--out", str(written), "--channels", "1",
                 "--apply"]) == 0
    assert written.exists()


@pytest.mark.needs_ffmpeg
def test_verify_exits_non_zero_on_a_pair_that_is_not_aligned(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """The exit code is the answer: a scan that fails has to say so to a script."""
    if "drift_pair.mka" not in media_fixtures:
        pytest.skip("the drifting pair was not built")
    source = str(media_fixtures["drift_pair.mka"])
    assert main([
        "verify", source, source, "--reference-stream", "0", "--other-stream", "1",
        "--work", str(tmp_path / "work"), "--step", "10",
    ]) == 1
