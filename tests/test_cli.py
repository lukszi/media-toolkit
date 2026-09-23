"""Both entry points, driven the way a person drives them.

The properties worth asserting about a command-line tool are not about its
output. They are: it exits with a code that means something, it says what it
needs when it cannot run, and the command that reads nothing is runnable by
somebody who has installed nothing.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jfkit import cli as jfkit_cli
from mkvkit import cli as mkvkit_cli
from mkvkit.langid.worker import TrackEvidence, WindowResult

SERIES = "/srv/media/series"


def _evidence(name: str, vector: dict[str, float], windows: int = 6) -> dict[str, object]:
    return TrackEvidence(
        path=f"{SERIES}/Northwind/{name}",
        stream_index=1,
        model="fixture",
        runtime_s=2700.0,
        windows=tuple(
            WindowResult(start_s=120.0 * (n + 1), probabilities=vector, speech_s=14.0)
            for n in range(windows)
        ),
    ).to_record()


@pytest.fixture
def results(tmp_path: Path) -> Path:
    path = tmp_path / "results.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(_evidence("a", {"eng": 0.99, "deu": 0.01})) + "\n")
        handle.write(json.dumps(_evidence("b", {"eng": 0.60, "deu": 0.40}, 3)) + "\n")
    return path


# ------------------------------------------------------------------ both tools
@pytest.mark.parametrize("module", [jfkit_cli, mkvkit_cli])
def test_no_command_prints_help_and_exits_two(
    module: object, capsys: pytest.CaptureFixture[str]
) -> None:
    assert module.main([]) == 2  # type: ignore[attr-defined]
    assert "usage" in capsys.readouterr().out


@pytest.mark.parametrize("module", [jfkit_cli, mkvkit_cli])
def test_the_version_is_reported(
    module: object, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_code:
        module.main(["--version"])  # type: ignore[attr-defined]
    assert exit_code.value.code == 0
    assert "0." in capsys.readouterr().out


@pytest.mark.parametrize("module", [jfkit_cli, mkvkit_cli])
def test_both_tools_share_the_same_switches(module: object) -> None:
    """A script driving both should have to learn one set."""
    parser = module.build_parser()  # type: ignore[attr-defined]
    options = {action.dest for action in parser._actions}
    assert {"config", "verbose", "quiet", "log_file", "log_json"} <= options


# ------------------------------------------------------------- the file naming
def test_the_naming_command_exits_non_zero_on_a_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exit code is the point: this runs in a pipeline, before a rename."""
    ranged = tmp_path / "Hollowmere 2-14 The Ferry.avi"
    ranged.write_bytes(b"")
    assert jfkit_cli.main(["naming", str(ranged)]) == 1
    printed = capsys.readouterr().out
    assert "RANGE" in printed
    assert "12.1" in printed  # the version it was checked against, every time


def test_the_naming_command_exits_zero_on_a_clean_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "Northwind - S02E14 - The Ferry.mkv"
    clean.write_bytes(b"")
    assert jfkit_cli.main(["naming", str(clean)]) == 0
    assert "RANGE" not in capsys.readouterr().out


def test_the_naming_command_walks_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "Northwind - S01E01.mkv").write_bytes(b"")
    (tmp_path / "Northwind - S01E02.mkv").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    assert jfkit_cli.main(["naming", str(tmp_path)]) == 0
    assert "2 path(s)" in capsys.readouterr().out


# ------------------------------------------------------------------ the langid
def test_the_report_runs_with_nothing_installed(
    results: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No model, no probe, no server: it reads a file and decides."""
    assert mkvkit_cli.main(["langid", "report", "--results", str(results)]) == 0
    printed = capsys.readouterr().out
    assert "# Spoken-language pass" in printed
    assert "## What did not settle" in printed


def test_the_report_writes_both_renderings(results: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert mkvkit_cli.main(
        ["langid", "report", "--results", str(results), "--out-dir", str(out)]
    ) == 0
    assert (out / "langid-report.md").is_file()
    rows = (out / "langid-results.tsv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3  # a header and two tracks


def test_a_sub_command_with_no_verb_prints_its_help(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert mkvkit_cli.main(["langid"]) == 2
    assert "VERB" in capsys.readouterr().out


def test_a_missing_optional_dependency_names_the_install_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error a user can act on, rather than an import failure three frames down."""
    import mkvkit.errors as errors

    def refuse(name: str, *, extra: str, package: str = "mkvkit") -> None:
        raise errors.ExtraRequired(name, extra=extra, package=package)

    monkeypatch.setattr(errors, "require_module", refuse)
    monkeypatch.setattr("mkvkit.langid.worker.require_module", refuse)
    jobs = tmp_path / "jobs.jsonl"
    jobs.write_text(
        json.dumps({"path": f"{SERIES}/a.mkv", "stream_index": 1, "audio_ord": 0}) + "\n",
        encoding="utf-8",
    )
    code = mkvkit_cli.main(
        ["langid", "scan", "--jobs", str(jobs), "--out", str(tmp_path / "r.jsonl")]
    )
    assert code == 2
    assert "pip install" in capsys.readouterr().out


def test_the_registry_only_contains_what_could_be_loaded() -> None:
    parser = mkvkit_cli.build_parser()
    assert "langid" in mkvkit_cli.REGISTRY
    assert parser.prog == "mkvkit"
