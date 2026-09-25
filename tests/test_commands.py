"""The file-side sub-commands, driven the way a person drives them.

What is asserted here is not the wording of the output. It is that the
commands exist, that the ones that write default to writing nothing, and that
the exit code carries the answer -- because these belong in a pipeline, where
"it printed something" is not a signal.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mkvkit import cli as mkvkit_cli
from mkvkit.chapters.xml import Chapter, ChapterSet, build, parse

VERBS = ("probe", "chapters", "tags", "propedit", "verify", "remux", "swap")


# ------------------------------------------------------------------ the surface
@pytest.mark.parametrize("verb", VERBS)
def test_every_file_side_verb_is_registered(verb: str) -> None:
    parser = mkvkit_cli.build_parser()
    names = parser.format_help()
    assert verb in names


@pytest.mark.parametrize("verb", ["chapters", "tags"])
def test_a_group_with_no_verb_prints_its_help_and_exits_two(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert mkvkit_cli.main([verb]) == 2
    assert "usage" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["propedit", "remux", "swap"])
def test_every_writing_verb_offers_both_states_and_neither_third(verb: str) -> None:
    parser = mkvkit_cli.build_parser()
    help_text = _subparser_help(parser, verb)
    assert "--dry-run" in help_text
    assert "--apply" in help_text


def _subparser_help(parser: object, verb: str) -> str:
    for action in parser._actions:  # type: ignore[attr-defined]
        if hasattr(action, "choices") and action.choices and verb in action.choices:
            return str(action.choices[verb].format_help())
    raise AssertionError(verb)


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_probe_prints_the_tracks_and_their_identifiers(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(["probe", str(media_fixtures["tiny_multitrack.mkv"])])
    out = capsys.readouterr().out
    assert code == 0
    assert "uid" in out
    assert "seek index: yes" in out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_probe_exits_non_zero_on_a_container_that_is_not_what_it_says(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(["probe", str(media_fixtures["not_really_mkv.mkv"])])
    assert code == 1
    assert "PROBLEM" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_chapters_show_lists_the_marks(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(["chapters", "show", str(media_fixtures["chapter_grid.mkv"])])
    assert code == 0
    assert "12 mark(s)" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_chapters_rollback_writes_the_document_that_undoes_a_naming_pass(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "rollback.xml"
    code = mkvkit_cli.main(
        ["chapters", "rollback", str(media_fixtures["chapter_grid.mkv"]),
         "--out", str(out)]
    )
    assert code == 0
    assert "<ChapterDisplay>" not in out.read_text(encoding="utf-8")


def test_chapters_check_exits_non_zero_on_a_document_that_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "marks.xml"
    document.write_text(
        build(ChapterSet((Chapter(0), Chapter(0)))), encoding="utf-8", newline="\n"
    )
    assert mkvkit_cli.main(["chapters", "check", str(document)]) == 1
    assert "duplicate" in capsys.readouterr().out


def test_chapters_check_passes_a_good_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "marks.xml"
    marks = ChapterSet(
        (Chapter(0, "The harbour at dawn"), Chapter(120_000_000_000, "Out again"))
    )
    document.write_text(build(marks), encoding="utf-8", newline="\n")
    assert mkvkit_cli.main(["chapters", "check", str(document)]) == 0


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_tags_show_reports_a_tag_that_overrules_a_header(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(["tags", "show", str(media_fixtures["tag_override.mkv"])])
    out = capsys.readouterr().out
    assert code == 1
    assert "LANGUAGE" in out
    assert "PROBLEM" in out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_propedit_without_apply_changes_nothing(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mkvkit.probe import probe

    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    uid = probe(path).audio[2].uid
    assert uid is not None
    before = path.read_bytes()
    code = mkvkit_cli.main(
        ["propedit", str(path), "--track", str(uid), "--language", "spa"]
    )
    assert code == 0
    assert "dry run" in capsys.readouterr().out
    assert path.read_bytes() == before


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_propedit_with_apply_writes_and_says_what_changed(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.probe import probe

    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    uid = probe(path).audio[2].uid
    assert uid is not None
    code = mkvkit_cli.main(
        ["propedit", str(path), "--track", str(uid), "--language", "spa", "--apply"]
    )
    assert code == 0
    assert probe(path).audio[2].language == "spa"


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_verify_compares_a_file_with_its_copy(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], copy)
    code = mkvkit_cli.main(
        ["verify", str(media_fixtures["tiny_multitrack.mkv"]), str(copy)]
    )
    assert code == 0
    assert "PASS" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_verify_fails_when_the_difference_was_not_declared(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(
        [
            "verify",
            str(media_fixtures["tiny_multitrack.mkv"]),
            str(media_fixtures["chapter_grid.mkv"]),
        ]
    )
    assert code == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_remux_needs_a_staging_directory(
    media_fixtures: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(["remux", str(media_fixtures["tiny_multitrack.mkv"])])
    assert code == 2
    assert "staging" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_remux_without_apply_builds_nothing(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = mkvkit_cli.main(
        ["remux", str(media_fixtures["tiny_multitrack.mkv"]),
         "--staging", str(tmp_path), "--original-language", "eng"]
    )
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    assert "keep" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_remux_with_apply_verifies_the_rebuild_against_the_plan(
    media_fixtures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The comparison is part of the verb, not a hint printed after it."""
    config = tmp_path / "mkvkit.toml"
    config.write_text(
        '[policy]\nkeep_languages = ["eng", "deu"]\ndroppable_languages = ["fra"]\n'
        'default_audio = "deu"\n',
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    code = mkvkit_cli.main(
        ["--config", str(config), "remux", str(media_fixtures["tiny_multitrack.mkv"]),
         "--staging", str(staging), "--original-language", "eng", "--apply"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS (tracks dropped)" in out
    assert "--dropped 3" in out
    assert "--default-moved" in out
    assert sorted(p.name for p in staging.iterdir()) == ["tiny_multitrack.mkv"]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_remux_with_apply_fails_when_the_rebuild_does_not_verify(
    media_fixtures: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mkvkit import verify as verify_module

    def always_fails(*_args: object, **_kwargs: object) -> verify_module.Comparison:
        return verify_module.Comparison(problems=("a planted failure",), label="x")

    monkeypatch.setattr(verify_module, "compare", always_fails)
    config = tmp_path / "mkvkit.toml"
    config.write_text(
        '[policy]\nkeep_languages = ["eng"]\ndroppable_languages = ["fra"]\n',
        encoding="utf-8",
    )
    code = mkvkit_cli.main(
        ["--config", str(config), "remux", str(media_fixtures["tiny_multitrack.mkv"]),
         "--staging", str(tmp_path / "staging"), "--original-language", "eng",
         "--apply"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "did not verify" in out


def test_swap_needs_a_parking_directory(capsys: pytest.CaptureFixture[str]) -> None:
    code = mkvkit_cli.main(["swap", "/srv/media/movies/a.mkv", "/srv/staging/a.mkv"])
    assert code == 2
    assert "parked" in capsys.readouterr().out


def test_swap_without_apply_moves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keeper = tmp_path / "live" / "example.mkv"
    keeper.parent.mkdir()
    keeper.write_bytes(b"the original")
    replacement = tmp_path / "staged" / "example.mkv"
    replacement.parent.mkdir()
    replacement.write_bytes(b"the rebuild")
    code = mkvkit_cli.main(
        ["swap", str(keeper), str(replacement), "--parked", str(tmp_path / "parked")]
    )
    assert code == 0
    assert keeper.read_bytes() == b"the original"
    assert "not swapped" in capsys.readouterr().out


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_chapters_apply_leaves_the_previous_document_in_the_rollback_directory(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.chapters.xml import read_chapters

    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["chapter_grid.mkv"], path)
    before = read_chapters(path)
    document = tmp_path / "names.xml"
    document.write_text(
        build(ChapterSet((Chapter(0, "The harbour at dawn"), Chapter(2_000_000_000)))),
        encoding="utf-8", newline="\n",
    )
    rollback = tmp_path / "rollback"
    code = mkvkit_cli.main(
        ["chapters", "apply", str(path), "--document", str(document), "--apply",
         "--rollback-dir", str(rollback)]
    )
    assert code == 0
    saved = next(rollback.glob("*.chapters.xml")).read_text(encoding="utf-8")
    assert len(parse(saved)) == len(before) == 12
    assert len(list(rollback.glob("*.rollback.tsv"))) == 1


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_propedit_with_apply_writes_its_rollback_where_it_is_told(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    from mkvkit.probe import probe

    path = tmp_path / "example.mkv"
    shutil.copyfile(media_fixtures["tiny_multitrack.mkv"], path)
    uid = probe(path).audio[2].uid
    rollback = tmp_path / "rollback"
    code = mkvkit_cli.main(
        ["propedit", str(path), "--track", str(uid), "--language", "spa", "--apply",
         "--rollback-dir", str(rollback)]
    )
    assert code == 0
    tsv = next(rollback.glob("*.rollback.tsv")).read_text(encoding="utf-8")
    assert f"{uid}\tlanguage\tfre" in tsv or f"{uid}\tlanguage\tfra" in tsv
