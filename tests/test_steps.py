"""Plan, dry run, apply, audit, resume: the shared shape of every multi-step change."""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mkvkit.cli import main as mkvkit_main
from mkvkit.steps import (
    FILE_ACTIONS,
    Audit,
    FunctionAction,
    Plan,
    Step,
    apply,
    load_plan,
    read_audit,
    rename_steps,
    status,
)

VIDEO = "Northwind - S01E03 - The Quiet Harbour.mkv"


def _files(folder: Path, *names: str) -> list[Path]:
    out = []
    for name in names:
        path = folder / name
        path.write_text(name, encoding="utf-8")
        out.append(path)
    return out


def test_a_plan_renders_saves_and_reads_back_unchanged(tmp_path: Path) -> None:
    plan = Plan("rename", tuple(rename_steps([(tmp_path / "a.nfo", tmp_path / "b.nfo")])),
                notes=("one note",))
    text = plan.render()
    assert plan.fingerprint in text and "rename" in text and "note: one note" in text
    again = load_plan(plan.save(tmp_path / "plan.json"))
    assert again.fingerprint == plan.fingerprint
    assert again.steps == plan.steps


def test_a_plan_edited_after_it_was_saved_is_refused(tmp_path: Path) -> None:
    saved = Plan("rename", tuple(rename_steps([("a", "b")]))).save(tmp_path / "p.json")
    raw = json.loads(saved.read_text(encoding="utf-8"))
    raw["steps"][0]["params"]["dst"] = "elsewhere"
    saved.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        load_plan(saved)


def test_step_ids_are_unique() -> None:
    with pytest.raises(ValueError, match="twice"):
        Plan("x", (Step("a", "mkdir"), Step("a", "mkdir")))


def test_apply_runs_every_step_and_audits_each_one(tmp_path: Path) -> None:
    sources = _files(tmp_path, "one.nfo", "two.nfo")
    plan = Plan("rename", tuple(rename_steps(
        [(src, src.with_name("new-" + src.name)) for src in sources])))
    report = apply(plan, FILE_ACTIONS, audit=tmp_path / "audit.jsonl")
    assert report.ok, str(report)
    assert sorted(p.name for p in tmp_path.glob("new-*")) == ["new-one.nfo", "new-two.nfo"]
    events = [(e["event"], e.get("step")) for e in read_audit(tmp_path / "audit.jsonl")]
    assert events == [
        ("plan", None),
        ("start", "rename:0001"), ("done", "rename:0001"),
        ("start", "rename:0002"), ("done", "rename:0002"),
    ]


def test_a_failure_half_way_stops_and_a_second_run_resumes(tmp_path: Path) -> None:
    """The access-denied-in-the-middle case, resumed without writing anything by hand."""
    sources = _files(tmp_path, "a.nfo", "b.nfo", "c.nfo")
    plan = Plan("rename", tuple(rename_steps(
        [(src, src.with_suffix(".txt")) for src in sources])))
    denied = {"b.nfo"}
    calls: list[str] = []
    real = FILE_ACTIONS["rename"]

    def flaky(step: Step) -> Mapping[str, Any] | None:
        calls.append(step.id)
        if Path(step.params["src"]).name in denied:
            raise PermissionError("access is denied")
        return real.run(step)

    actions = {"rename": FunctionAction(flaky, real.done)}
    audit = tmp_path / "audit.jsonl"
    first = apply(plan, actions, audit=audit)
    assert not first.ok
    assert [r.state for r in first.results] == ["done", "failed", "pending"]
    assert "resume" in str(first) and "PermissionError" in str(first)
    assert status(plan, read_audit(audit)) == {
        "rename:0001": "done", "rename:0002": "failed", "rename:0003": "pending"}

    denied.clear()
    calls.clear()
    second = apply(plan, actions, audit=audit)
    assert second.ok, str(second)
    assert [r.state for r in second.results] == ["skipped", "done", "done"]
    assert calls == ["rename:0002", "rename:0003"]
    assert sorted(p.name for p in tmp_path.glob("*.txt")) == ["a.txt", "b.txt", "c.txt"]


def test_a_step_finished_before_the_audit_could_say_so_is_not_run_again(
    tmp_path: Path,
) -> None:
    (src,) = _files(tmp_path, "a.nfo")
    plan = Plan("rename", tuple(rename_steps([(src, tmp_path / "b.nfo")])))
    os.rename(src, tmp_path / "b.nfo")  # a crash between the rename and the audit line
    report = apply(plan, FILE_ACTIONS, audit=tmp_path / "audit.jsonl")
    assert report.ok
    assert report.results[0].detail == {"already": "the change was in place"}


def test_an_audit_belongs_to_one_plan(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    (src,) = _files(tmp_path, "a.nfo")
    apply(Plan("rename", tuple(rename_steps([(src, tmp_path / "b.nfo")]))),
          FILE_ACTIONS, audit=audit)
    other = Plan("mkdir", (Step("rename:0001", "mkdir", {"path": str(tmp_path / "d")}),))
    assert status(other, read_audit(audit)) == {"rename:0001": "pending"}


def test_a_transient_refusal_is_retried_when_asked(tmp_path: Path) -> None:
    tries: list[int] = []

    def held_once(step: Step) -> Mapping[str, Any] | None:
        tries.append(1)
        if len(tries) == 1:
            raise PermissionError("held open by another program")
        return {"ok": True}

    waits: list[float] = []
    report = apply(Plan("x", (Step("s1", "held"),)), {"held": FunctionAction(held_once)},
                   attempts=2, backoff_s=0.5, sleep=waits.append)
    assert report.ok and waits == [0.5]


def test_a_rename_never_replaces_anything(tmp_path: Path) -> None:
    src, dst = _files(tmp_path, "a.nfo", "b.nfo")
    report = apply(Plan("rename", tuple(rename_steps([(src, dst)]))), FILE_ACTIONS,
                   resume=False)
    assert not report.ok and "FileExistsError" in (report.results[0].error or "")
    assert dst.read_text(encoding="utf-8") == "b.nfo" and src.exists()


def test_a_folder_is_renamed_too(tmp_path: Path) -> None:
    folder = tmp_path / "old.trickplay"
    folder.mkdir()
    (folder / "tile.jpg").write_bytes(b"x")
    report = apply(Plan("rename", tuple(rename_steps([(folder, tmp_path / "new.trickplay")]))),
                   FILE_ACTIONS)
    assert report.ok and (tmp_path / "new.trickplay" / "tile.jpg").is_file()


def test_an_unknown_action_is_refused_before_anything_runs(tmp_path: Path) -> None:
    (src,) = _files(tmp_path, "a.nfo")
    plan = Plan("x", (*rename_steps([(src, tmp_path / "b.nfo")]), Step("z", "teleport")))
    with pytest.raises(KeyError, match="teleport"):
        apply(plan, FILE_ACTIONS)
    assert src.exists()


def test_the_audit_is_kept_in_memory_without_a_path() -> None:
    audit = Audit()
    apply(Plan("x", (Step("s", "noop"),)), {"noop": FunctionAction(lambda s: None)},
          audit=audit)
    assert [e["event"] for e in audit.entries] == ["plan", "start", "done"]


def test_a_torn_last_audit_line_is_ignored(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text('{"event": "plan"}\n{"event": "sta', encoding="utf-8")
    assert read_audit(audit) == [{"event": "plan"}]


def test_the_verb_shows_dry_runs_applies_and_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    video = tmp_path / VIDEO
    video.write_bytes(b"video")
    plan = Plan("rename", tuple(rename_steps([(video, tmp_path / "result.mkv")])))
    saved = plan.save(tmp_path / "plan.json")
    audit = tmp_path / "audit.jsonl"

    assert mkvkit_main(["steps", "show", str(saved)]) == 0
    assert plan.fingerprint in capsys.readouterr().out
    assert mkvkit_main(["steps", "apply", str(saved), "--audit", str(audit)]) == 1
    assert "dry run" in capsys.readouterr().out and video.exists()
    assert mkvkit_main(["steps", "apply", str(saved), "--audit", str(audit), "--apply"]) == 0
    assert (tmp_path / "result.mkv").is_file() and not video.exists()
    assert mkvkit_main(["steps", "status", str(saved), "--audit", str(audit)]) == 0
    assert "done" in capsys.readouterr().out


def test_the_verb_refuses_a_plan_with_steps_it_cannot_run(tmp_path: Path) -> None:
    saved = Plan("x", (Step("s", "userdata.write"),)).save(tmp_path / "plan.json")
    assert mkvkit_main(["steps", "apply", str(saved), "--audit",
                        str(tmp_path / "a.jsonl"), "--apply"]) == 2
