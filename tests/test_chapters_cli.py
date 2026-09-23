"""The chapter-name verbs, driven the way a person drives them.

What is asserted is what matters about a command-line tool: the exit code
means something, reading is free, writing is asked for twice, and the verb
that judges a name list runs on a machine that has never been near the media.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mkvkit import cli as mkvkit_cli
from mkvkit import commands as commands_module
from mkvkit.chapters.xml import Chapter, ChapterSet, build
from mkvkit.probe import Container, MediaProbe

SECOND = 1_000_000_000
RUNTIME_S = 1200.0
MARKS_S = (0.0, 300.0, 600.0, 900.0)
NAMES = (
    "The Harbour at Dawn",
    "A Letter from the Coast",
    "The Long Drive North",
    "Rain on the Quarry Road",
)
LINES = (
    "the harbour is quiet at dawn and the boats have not gone out",
    "a letter came up from the coast this morning addressed to nobody",
    "we drive north all night and the road never seems to end",
    "rain on the quarry road turns the whole hillside into mud",
)


def marks(names=None, offset: float = 0.0) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(round((t + offset) * SECOND), None if names is None else names[i])
            for i, t in enumerate(MARKS_S)
        )
    )


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A file whose marks and runtime are answered without running anything."""
    path = tmp_path / "example.mkv"
    path.write_bytes(b"not read: the programs are stood in for")

    def read_chapters(target, *, config=None, runner=None):  # type: ignore[no-untyped-def]
        return marks()

    def probe(target, *, config=None, runner=None, elements=True):  # type: ignore[no-untyped-def]
        return MediaProbe(
            path=Path(target),
            container=Container(type="Matroska", duration_s=RUNTIME_S),
        )

    monkeypatch.setattr(commands_module.chapters_xml, "read_chapters", read_chapters)
    monkeypatch.setattr(commands_module, "probe", probe)
    return path


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    path = tmp_path / "candidate.xml"
    path.write_text(build(marks(NAMES, offset=0.7)), encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def cues(tmp_path: Path) -> Path:
    """A transcript dense enough and long enough to be usable."""
    blocks = []
    for index in range(120):
        at = index * 10.0
        start = f"{int(at // 3600):02d}:{int(at // 60) % 60:02d}:{int(at % 60):02d},000"
        end = f"{int(at // 3600):02d}:{int(at // 60) % 60:02d}:{int(at % 60) + 4:02d},000"
        text = LINES[min(int(at // 300), 3)]
        blocks.append(f"{index + 1}\n{start} --> {end}\n{text}\n")
    path = tmp_path / "cues.srt"
    path.write_text("\n".join(blocks), encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    path = tmp_path / "evidence.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, (at, name, line) in enumerate(zip(MARKS_S, NAMES, LINES, strict=True), start=1):
            handle.write(
                json.dumps(
                    {"index": index, "time_s": at, "name": name, "transcript": line}
                )
                + "\n"
            )
    return path


# ------------------------------------------------------------------- classify
def test_classify_names_the_job_and_exits_zero(
    media: Path, candidate: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(
        ["chapters", "classify", str(media), "--candidate", str(candidate)]
    )
    assert code == 0
    assert "names-only" in capsys.readouterr().out


def test_classify_exits_non_zero_on_a_candidate_for_another_cut(
    media: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.xml"
    other.write_text(
        build(
            ChapterSet(
                tuple(Chapter(round(i * 411.0 * SECOND), NAMES[i]) for i in range(4))
            )
        ),
        encoding="utf-8", newline="\n",
    )
    assert mkvkit_cli.main(
        ["chapters", "classify", str(media), "--candidate", str(other)]
    ) == 1
    assert "not-usable" in capsys.readouterr().out


# ---------------------------------------------------------------------- match
def test_match_writes_the_document_it_accepted(
    media: Path, candidate: Path, tmp_path: Path
) -> None:
    out = tmp_path / "result.xml"
    code = mkvkit_cli.main(
        ["chapters", "match", str(media), "--candidate", str(candidate),
         "--out", str(out)]
    )
    assert code == 0
    written = out.read_text(encoding="utf-8")
    assert "The Harbour at Dawn" in written
    assert "00:05:00.000000000" in written  # the file's own mark, not the candidate's


def test_match_with_evidence_that_disagrees_exits_non_zero(
    media: Path, candidate: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slid = tmp_path / "evidence.jsonl"
    with slid.open("w", encoding="utf-8", newline="\n") as handle:
        for index, at in enumerate(MARKS_S, start=1):
            handle.write(
                json.dumps({
                    "index": index, "time_s": at,
                    "name": NAMES[index - 1],
                    "transcript": LINES[(index + 1) % 4],
                }) + "\n"
            )
    code = mkvkit_cli.main(
        ["chapters", "match", str(media), "--candidate", str(candidate),
         "--evidence", str(slid)]
    )
    assert code == 1
    assert "refused" in capsys.readouterr().out


def test_match_with_evidence_that_agrees_exits_zero(
    media: Path, candidate: Path, evidence: Path
) -> None:
    assert mkvkit_cli.main(
        ["chapters", "match", str(media), "--candidate", str(candidate),
         "--evidence", str(evidence)]
    ) == 0


# -------------------------------------------------------------------- windows
def test_windows_prints_one_per_mark_and_no_timestamp(
    media: Path, cues: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert mkvkit_cli.main(
        ["chapters", "windows", str(media), "--cues", str(cues)]
    ) == 0
    out = capsys.readouterr().out
    assert "WINDOWS: 4" in out
    assert "[4]" in out
    assert "00:05:00" not in out


def test_windows_refuses_a_transcript_that_is_a_stub(
    media: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    thin = tmp_path / "thin.srt"
    thin.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nonly one cue in the whole film\n",
        encoding="utf-8", newline="\n",
    )
    assert mkvkit_cli.main(
        ["chapters", "windows", str(media), "--cues", str(thin)]
    ) == 1
    assert "refused" in capsys.readouterr().out


def test_windows_written_twice_is_written_once(
    media: Path, cues: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "windows.txt"
    mkvkit_cli.main(
        ["chapters", "windows", str(media), "--cues", str(cues), "--out", str(out)]
    )
    capsys.readouterr()
    mkvkit_cli.main(
        ["chapters", "windows", str(media), "--cues", str(cues), "--out", str(out)]
    )
    assert "already current" in capsys.readouterr().out


# ------------------------------------------------------------------ selfcheck
def write_names(path: Path, names) -> Path:  # type: ignore[no-untyped-def]
    path.write_text(json.dumps({"names": list(names)}), encoding="utf-8", newline="\n")
    return path


def test_selfcheck_passes_names_that_describe_their_windows(
    media: Path, cues: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = write_names(tmp_path / "names.json", NAMES)
    assert mkvkit_cli.main(
        ["chapters", "selfcheck", str(media), "--cues", str(cues),
         "--names", str(names)]
    ) == 0
    assert "clean" in capsys.readouterr().out


def test_selfcheck_holds_a_film_back_on_one_wrong_name(
    media: Path, cues: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names = write_names(
        tmp_path / "names.json", ("Arrival at 12:40", *NAMES[1:])
    )
    assert mkvkit_cli.main(
        ["chapters", "selfcheck", str(media), "--cues", str(cues),
         "--names", str(names)]
    ) == 1
    assert "flagged" in capsys.readouterr().out


def test_selfcheck_refuses_a_name_list_of_the_wrong_length(
    media: Path, cues: Path, tmp_path: Path
) -> None:
    names = write_names(tmp_path / "names.json", NAMES[:2])
    assert mkvkit_cli.main(
        ["chapters", "selfcheck", str(media), "--cues", str(cues),
         "--names", str(names)]
    ) == 1


# ----------------------------------------------------------------------- plan
def test_plan_defaults_to_a_dry_run(
    media: Path, candidate: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(
        commands_module.tags_module, "read_tags",
        lambda path, config=None, runner=None: commands_module.tags_module.TagSet(),
    )
    monkeypatch.setattr(
        commands_module.plan_module, "apply",
        lambda plan, *, dry_run=True, runner=None, config=None: (
            seen.append(dry_run)
            or commands_module.plan_module.PropeditResult(plan.path)
        ),
    )
    code = mkvkit_cli.main(
        ["chapters", "plan", str(media), "--candidate", str(candidate),
         "--source", "an example chapter archive"]
    )
    assert code == 0
    assert seen == [True]
    assert "would change" in capsys.readouterr().out


def test_plan_writes_only_when_asked_twice(
    media: Path, candidate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(
        commands_module.tags_module, "read_tags",
        lambda path, config=None, runner=None: commands_module.tags_module.TagSet(),
    )
    monkeypatch.setattr(
        commands_module.plan_module, "apply",
        lambda plan, *, dry_run=True, runner=None, config=None: (
            seen.append(dry_run)
            or commands_module.plan_module.PropeditResult(plan.path)
        ),
    )
    mkvkit_cli.main(
        ["chapters", "plan", str(media), "--candidate", str(candidate),
         "--source", "an example chapter archive", "--apply"]
    )
    assert seen == [False]


def test_plan_exits_non_zero_when_the_candidate_is_refused(
    media: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.xml"
    other.write_text(
        build(
            ChapterSet(
                tuple(Chapter(round(i * 411.0 * SECOND), NAMES[i]) for i in range(4))
            )
        ),
        encoding="utf-8", newline="\n",
    )
    assert mkvkit_cli.main(
        ["chapters", "plan", str(media), "--candidate", str(other),
         "--source", "an example chapter archive"]
    ) == 1
    assert "refused" in capsys.readouterr().out


def test_every_new_verb_is_registered() -> None:
    parser = mkvkit_cli.build_parser()
    chapters = [
        action for action in parser._subparsers._group_actions  # type: ignore[union-attr]
        for name, action in action.choices.items() if name == "chapters"
    ]
    assert chapters, "the chapters sub-command is not registered"
    verbs = set()
    for action in chapters[0]._subparsers._group_actions:  # type: ignore[union-attr]
        verbs |= set(action.choices)
    assert {"classify", "match", "windows", "selfcheck", "plan"} <= verbs
