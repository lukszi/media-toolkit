"""One file, everything that would change to it, and the churn between runs.

The plan is what a dry run prints and what the editor executes, so the tests
here are about three things: that a refusal survives as part of the plan
rather than as an exception, that every pending edit reaches the file in one
invocation, and that two runs of the same pass can be compared.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mkvkit.chapters.names import match_names
from mkvkit.chapters.xml import Chapter, ChapterSet
from mkvkit.plan import FilePlan, apply, chapter_names_plan, explain_churn, summarise
from mkvkit.run import Result
from mkvkit.tags import SimpleTag, Tag, TagSet, Targets

SECOND = 1_000_000_000
TRACK_UID = 2222
NAMES = (
    "The Harbour at Dawn",
    "A Letter from the Coast",
    "The Long Drive North",
    "Rain on the Quarry Road",
)


def marks(names=None, offset: float = 0.0) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(round((i * 300.0 + offset) * SECOND),
                    None if names is None else names[i])
            for i in range(4)
        )
    )


def identified(chapters: int = 4) -> dict[str, Any]:
    return {
        "container": {"type": "Matroska", "properties": {"duration": 1_200_000_000_000}},
        "tracks": [
            {
                "id": 0, "type": "audio", "codec": "FLAC",
                "properties": {
                    "codec_id": "A_FLAC", "number": 1, "uid": TRACK_UID,
                    "language": "eng", "default_track": True,
                    "enabled_track": True, "audio_channels": 2,
                },
            }
        ],
        "chapters": [{"num_entries": chapters}] if chapters else [],
    }


class StandIn:
    """Answers for each program; records every invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(
        self, tool: str, args: Sequence[str | Path], *, ok: Sequence[int] = (0,)
    ) -> Result:
        argv = tuple(str(a) for a in args)
        self.calls.append((tool, argv))
        if tool == "mkvmerge":
            return Result(tool, argv, 0, json.dumps(identified()), "")
        if tool == "ffprobe":
            return Result(
                tool, argv, 0,
                json.dumps({
                    "streams": [{"index": 0, "codec_type": "audio",
                                 "codec_name": "flac", "channels": 2}],
                    "format": {"duration": "1200.0", "size": "1024"},
                }),
                "",
            )
        if tool == "mkvextract":
            return Result(tool, argv, 0, "", "")
        return Result(tool, argv, 0, "", "")

    @property
    def edits(self) -> list[tuple[str, ...]]:
        return [argv for tool, argv in self.calls if tool == "mkvpropedit"]


def target(tmp_path: Path) -> Path:
    path = tmp_path / "example.mkv"
    path.write_bytes(b"not read: the programs are stood in for")
    return path


# ------------------------------------------------------------------- the shape
def test_an_empty_plan_says_so_rather_than_looking_broken() -> None:
    plan = FilePlan(Path("/srv/media/movies/example.mkv"))
    assert plan.empty
    assert not plan.blocked
    assert "nothing would change" in str(plan)


def test_a_plan_lists_every_change_it_would_make() -> None:
    plan = FilePlan(
        Path("/srv/media/movies/example.mkv"),
        chapters=marks(NAMES),
        tags=TagSet((Tag(Targets(), (SimpleTag("CHAPTER_NAMES_SOURCE", "somewhere"),)),)),
        title="The Quiet Harbour",
    )
    text = str(plan)
    assert "4 mark(s), 4 named" in text
    assert "1 tag(s)" in text
    assert "The Quiet Harbour" in text
    assert len(plan.changes) == 3


def test_a_refusal_is_part_of_the_plan_rather_than_an_exception() -> None:
    plan = FilePlan(Path("/srv/media/movies/example.mkv"), chapters=marks(NAMES))
    refused = plan.refuse("the grids do not describe the same cut")
    assert refused.blocked
    assert refused.chapters is None
    assert "refused:" in str(refused)


# ---------------------------------------------------------------- the builder
def test_the_names_and_their_provenance_go_in_together(tmp_path: Path) -> None:
    result = match_names(marks(), marks(NAMES, offset=0.6))
    plan = chapter_names_plan(
        target(tmp_path), result, source="an example chapter archive"
    )
    assert plan.chapters is not None
    assert plan.tags is not None
    names = {
        simple.name for tag in plan.tags.tags for simple in tag.simples
    }
    assert "CHAPTER_NAMES_SOURCE" in names


def test_a_tag_the_file_already_carries_survives(tmp_path: Path) -> None:
    """One of them may be the only thing making the file read correctly."""
    existing = TagSet(
        (Tag(Targets(track_uids=(TRACK_UID,)), (SimpleTag("LANGUAGE", "deu"),)),)
    )
    result = match_names(marks(), marks(NAMES, offset=0.6))
    plan = chapter_names_plan(
        target(tmp_path), result, source="an example chapter archive",
        existing_tags=existing,
    )
    assert plan.tags is not None
    pairs = {
        (simple.name, simple.value)
        for tag in plan.tags.tags
        for simple in tag.simples
    }
    assert ("LANGUAGE", "deu") in pairs


def test_a_refused_match_becomes_a_refused_plan(tmp_path: Path) -> None:
    elsewhere = ChapterSet(
        tuple(Chapter(round(i * 411.0 * SECOND), NAMES[i]) for i in range(4))
    )
    result = match_names(marks(), elsewhere)
    plan = chapter_names_plan(target(tmp_path), result, source="an example archive")
    assert plan.blocked
    assert plan.chapters is None


# ------------------------------------------------------------------ the writing
def test_everything_pending_reaches_the_file_in_one_invocation(tmp_path: Path) -> None:
    """Two edits are two modification times, and a server watches those."""
    runner = StandIn()
    result = match_names(marks(), marks(NAMES, offset=0.6))
    plan = chapter_names_plan(target(tmp_path), result, source="an example archive")
    outcome = apply(plan, dry_run=False, runner=runner)
    assert outcome.applied
    assert len(runner.edits) == 1
    argv = runner.edits[0]
    assert "--chapters" in argv
    assert "--tags" in argv


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    runner = StandIn()
    result = match_names(marks(), marks(NAMES, offset=0.6))
    plan = chapter_names_plan(target(tmp_path), result, source="an example archive")
    outcome = apply(plan, dry_run=True, runner=runner)
    assert not outcome.applied
    assert runner.edits == []


def test_a_blocked_plan_is_not_attempted(tmp_path: Path) -> None:
    runner = StandIn()
    plan = FilePlan(target(tmp_path)).refuse("nothing about this candidate is usable")
    outcome = apply(plan, dry_run=False, runner=runner)
    assert not outcome.ok
    assert runner.calls == []


def test_an_empty_plan_is_not_attempted_either(tmp_path: Path) -> None:
    runner = StandIn()
    outcome = apply(FilePlan(target(tmp_path)), dry_run=False, runner=runner)
    assert outcome.ok
    assert not outcome.applied
    assert runner.calls == []


# -------------------------------------------------------------------- the churn
def one(name: str, **kwargs: Any) -> FilePlan:
    return FilePlan(Path(f"/srv/media/movies/{name}.mkv"), **kwargs)


def test_a_file_that_entered_the_plan_says_why() -> None:
    churn = explain_churn([], [one("first", chapters=marks(NAMES))])
    assert len(churn.entered) == 1
    assert "4 mark(s)" in churn.entered[0][1]


def test_a_file_that_left_the_plan_is_the_interesting_half() -> None:
    """A plain diff of two lists hides exactly this row."""
    churn = explain_churn([one("first", chapters=marks(NAMES))], [])
    assert len(churn.left) == 1
    assert churn.entered == ()


def test_a_file_whose_plan_changed_shows_both_sides() -> None:
    before = one("first", chapters=marks(NAMES))
    after = one("first").refuse("the evidence no longer supports these names")
    churn = explain_churn([before], [after])
    assert len(churn.changed) == 1
    assert "->" in churn.changed[0][1]


def test_an_unchanged_plan_is_counted_and_not_listed() -> None:
    plans = [one("first", chapters=marks(NAMES))]
    churn = explain_churn(plans, plans)
    assert churn.quiet
    assert churn.unchanged == 1


def test_the_summary_counts_the_three_outcomes() -> None:
    text = summarise([
        one("first", chapters=marks(NAMES)),
        one("second").refuse("no candidate fits"),
        one("third"),
    ])
    assert "1 file(s) would change, 1 refused, 1 already as they should be" in text
