"""Were these names typed against these marks?

The centre of this file is one scenario: a name list whose marks fit the cut
perfectly and whose names sit three marks away from the scenes they describe.
That is the finding the module exists for, and it is reproduced here from
invented material -- the same shape, with names and dialogue made up for the
purpose.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

import pytest
from mkvkit.chapters.verify import (
    ALIGNED,
    MISALIGNED,
    PLAUSIBLE,
    UNCERTAIN,
    UNCLEAR,
    WRONG,
    Corroboration,
    EvidenceProvider,
    MarkEvidence,
    Rubric,
    SingleSeekEvidence,
    call_marks,
    collect_evidence,
    content_words,
    corroborate,
    hit_rate,
    shift_score,
    verdict,
)
from mkvkit.chapters.xml import Chapter, ChapterSet

SECOND = 1_000_000_000

#: Eight invented chapter names and eight invented windows that belong to them.
NAMES = (
    "The Harbour at Dawn",
    "A Letter from the Coast",
    "The Long Drive North",
    "Rain on the Quarry Road",
    "What the Foreman Knew",
    "The Last Ferry",
    "Two Days in the Valley House",
    "The Lantern in the Window",
)
WINDOWS = (
    "the harbour is quiet at dawn and the boats have not gone out",
    "a letter came up from the coast this morning addressed to nobody",
    "we drive north all night and the road never seems to end",
    "rain on the quarry road turns the whole hillside into mud",
    "the foreman knew about the pumps and said nothing to anybody",
    "the last ferry leaves at six and we are not going to be on it",
    "two days in the valley house and the telephone has not rung once",
    "a lantern in the window means somebody is still waiting up",
)


def marks(count: int = 8) -> ChapterSet:
    return ChapterSet(
        tuple(Chapter(round(i * 300.0 * SECOND), NAMES[i]) for i in range(count))
    )


def evidence(names=NAMES, windows=WINDOWS) -> list[MarkEvidence]:
    return [
        MarkEvidence(index=i + 1, time_s=i * 300.0, name=names[i], transcript=windows[i])
        for i in range(len(names))
    ]


# ------------------------------------------------------------------ the words
def test_a_name_made_of_nothing_but_common_words_cannot_be_scored() -> None:
    assert content_words("The End") == []
    assert hit_rate("The End", "anything at all") is None


def test_a_label_carries_no_content_words() -> None:
    assert content_words("Chapter 7") == []


def test_a_structural_name_is_never_scored() -> None:
    assert content_words("End Credits") == []
    assert content_words("Vorspann") == []


def test_a_word_is_matched_through_its_ending() -> None:
    rate = hit_rate("The Waiting Room", "she was waiting in the room for hours")
    assert rate == (2, 2)


# ------------------------------------------------------------- the shift score
def test_an_aligned_list_scores_best_where_it_sits() -> None:
    scores = shift_score(list(NAMES), list(WINDOWS))
    assert scores.best_shift == 0
    assert scores.at_zero == pytest.approx(0.944, abs=0.01)
    assert not scores.misaligned(threshold=0.06)
    assert scores.strength == "strong"


def test_a_list_typed_against_other_marks_scores_better_somewhere_else() -> None:
    """The signature: the marks fit and the names sit three marks too early."""
    slid = [*NAMES[3:], *NAMES[:3]]
    scores = shift_score(slid, list(WINDOWS))
    assert scores.best_shift == 3
    assert scores.misaligned(threshold=0.06)
    assert scores.margin > 0.3


def test_names_that_share_no_word_with_anything_score_nothing_anywhere() -> None:
    """Weak everywhere is not an accusation; plenty of real lists look like this."""
    scores = shift_score(
        ["Quiet Bastion", "Hollow Ledger", "Winter Aviary"],
        ["nobody says any of these words", "nor here", "nor here either"],
    )
    assert scores.at_zero == 0.0
    assert scores.strength == "weak"
    assert not scores.misaligned(threshold=0.06)


def test_a_list_with_no_content_words_at_all_is_unscoreable() -> None:
    scores = shift_score(["Chapter 1", "Chapter 2"], ["something", "else"])
    assert scores.at_zero is None
    assert scores.strength == "no content words"
    assert scores.margin == 0.0


# -------------------------------------------------------------- the mark calls
def test_a_name_said_in_its_own_window_is_plausible() -> None:
    calls = call_marks(evidence())
    assert {call.call for call in calls} == {PLAUSIBLE}


def test_a_name_whose_scene_starts_elsewhere_is_wrong() -> None:
    names = list(NAMES)
    names[1] = NAMES[5]
    calls = call_marks(evidence(names=names))
    assert calls[1].call == WRONG
    assert calls[1].best_elsewhere == 6


def test_a_silent_window_is_unclear_rather_than_wrong() -> None:
    windows = list(WINDOWS)
    windows[4] = ""
    calls = call_marks(evidence(windows=windows))
    assert calls[4].call == UNCLEAR
    assert "nothing is said" in calls[4].reason


def test_a_vague_name_is_never_condemned_for_being_vague() -> None:
    """Disc authors write those on purpose."""
    names = list(NAMES)
    names[2] = "Continued"
    calls = call_marks(evidence(names=names))
    assert calls[2].call == UNCLEAR


def test_a_structural_name_where_one_belongs_is_plausible() -> None:
    names = list(NAMES)
    names[0], names[-1] = "Main Titles", "End Credits"
    calls = call_marks(evidence(names=names))
    assert calls[0].call == PLAUSIBLE
    assert calls[-1].call == PLAUSIBLE


def test_a_structural_name_in_the_middle_settles_nothing() -> None:
    names = list(NAMES)
    names[3] = "End Credits"
    assert call_marks(evidence(names=names))[3].call == UNCLEAR


def test_a_mark_that_could_not_be_collected_carries_its_reason() -> None:
    items = evidence()
    items[2] = MarkEvidence(3, 600.0, NAMES[2], note="not collected: no such stream")
    calls = call_marks(items)
    assert calls[2].call == UNCLEAR
    assert "no such stream" in calls[2].reason


# ----------------------------------------------------------- the film verdict
def test_a_clean_list_comes_out_aligned_and_is_writable() -> None:
    found = verdict(evidence(), shift_score(list(NAMES), list(WINDOWS)))
    assert found.verdict == ALIGNED
    assert found.writable
    assert found.counts[PLAUSIBLE] == 8


def test_the_shift_alone_is_enough_to_hold_a_film_back() -> None:
    slid = [*NAMES[3:], *NAMES[:3]]
    found = verdict(evidence(names=slid), shift_score(slid, list(WINDOWS)))
    assert found.verdict == MISALIGNED
    assert not found.writable
    assert "mark(s) away" in found.reason


def test_two_contradictions_are_enough_on_their_own() -> None:
    names = list(NAMES)
    names[1], names[2] = NAMES[5], NAMES[6]
    found = verdict(evidence(names=names))
    assert found.verdict == MISALIGNED


def test_one_contradiction_is_uncertain_rather_than_misaligned() -> None:
    names = list(NAMES)
    names[1] = NAMES[5]
    found = verdict(evidence(names=names))
    assert found.verdict == UNCERTAIN
    assert not found.writable


def test_too_little_to_check_is_uncertain_not_aligned() -> None:
    """Nothing to go on is a verdict, not a pass."""
    windows = ["", "", "", "", "", "", *WINDOWS[6:]]
    found = verdict(evidence(windows=windows))
    assert found.verdict == UNCERTAIN
    assert "could be checked" in found.reason


def test_the_bar_is_applied_here_and_not_by_whoever_made_the_calls() -> None:
    """A rubric sharpened halfway through re-runs over the calls it already has."""
    names = list(NAMES)
    names[1] = NAMES[5]
    items = evidence(names=names)
    calls = call_marks(items)
    strict = verdict(items, calls=calls, rubric=Rubric(max_wrong=0))
    assert strict.verdict == MISALIGNED


def test_no_marks_is_uncertain() -> None:
    assert verdict([]).verdict == UNCERTAIN


# ------------------------------------------------------------ a second opinion
def test_a_second_contributor_agreeing_is_corroboration() -> None:
    mine = marks()
    theirs = ChapterSet(
        tuple(Chapter(round(i * 300.0 * SECOND) + 500_000_000, NAMES[i])
              for i in range(8))
    )
    found = corroborate(mine, [("another entry", theirs)], marks=mine)
    assert found.agreeing == ("another entry",)
    assert found.same_name_order == 8
    assert found.corroborated


def test_a_list_published_for_a_different_cut_is_the_archives_not_the_films() -> None:
    """Identical names, different marks: the entry is not about this film."""
    mine = marks()
    elsewhere = ChapterSet(
        tuple(Chapter(round(i * 412.0 * SECOND), NAMES[i]) for i in range(8))
    )
    found = corroborate(mine, [("an entry for another title", elsewhere)], marks=mine)
    assert found.borrowed_from == ("an entry for another title",)
    assert not found.corroborated
    assert "not this film" in str(found)


def test_no_other_list_is_simply_no_other_list() -> None:
    assert str(Corroboration()) == "no other published list matches these marks"


# ------------------------------------------------------------- the collection
class FakeProvider:
    """Answers from a table and records what it was asked for."""

    def __init__(self, answers: dict[int, str], fail: set[int] | None = None) -> None:
        self.answers = answers
        self.fail = fail or set()
        self.asked: list[tuple[int, float]] = []

    def collect(self, media: Path, index: int, at_s: float) -> tuple[str, Path | None]:
        self.asked.append((index, at_s))
        if index in self.fail:
            raise RuntimeError("no such stream")
        return self.answers.get(index, ""), Path(f"mark{index:04d}.jpg")


def test_the_provider_protocol_is_satisfied_by_a_plain_object() -> None:
    assert isinstance(FakeProvider({}), EvidenceProvider)


def test_evidence_is_collected_once_per_mark_in_order() -> None:
    provider = FakeProvider({i + 1: WINDOWS[i] for i in range(8)})
    found = collect_evidence("/srv/media/movies/example.mkv", marks(), provider=provider)
    assert [index for index, _ in provider.asked] == list(range(1, 9))
    assert [e.transcript for e in found] == list(WINDOWS)
    assert [e.name for e in found] == list(NAMES)


def test_a_mark_that_fails_is_recorded_rather_than_dropped() -> None:
    """A dropped mark shifts every index after it, which is the failure mode."""
    provider = FakeProvider({i + 1: WINDOWS[i] for i in range(8)}, fail={3})
    found = collect_evidence("/srv/media/movies/example.mkv", marks(), provider=provider)
    assert len(found) == 8
    assert found[2].transcript == ""
    assert "no such stream" in found[2].note


def test_both_outputs_come_out_of_one_invocation(tmp_path: Path) -> None:
    """Two seeks per mark is what makes this too slow to run at all."""
    calls: list[tuple[str, list[str]]] = []

    def runner(tool, args, *, ok=(0,)):  # type: ignore[no-untyped-def]
        calls.append((tool, [str(a) for a in args]))
        (tmp_path / "work" / "mark0002.wav").write_bytes(b"")
        return None

    provider = SingleSeekEvidence(
        transcribe=lambda audio, language=None: "what was said",
        frames_dir=tmp_path / "frames",
        work_dir=tmp_path / "work",
        runner=runner,
        audio_stream=1,
    )
    transcript, frame = provider.collect(Path("/srv/media/movies/example.mkv"), 2, 61.5)
    assert transcript == "what was said"
    assert frame == tmp_path / "frames" / "mark0002.jpg"
    assert len(calls) == 1
    tool, args = calls[0]
    assert tool == "ffmpeg"
    assert args.count("-i") == 1
    assert args.count("-ss") == 2
    assert "0:a:1" in args
    assert "0:v:0" in args


def test_the_audio_window_starts_at_the_mark(tmp_path: Path) -> None:
    provider = SingleSeekEvidence(work_dir=tmp_path, seconds=20.0)
    args = provider.command(Path("/srv/media/movies/example.mkv"), 1, 1234.5)
    assert args[args.index("-ss") + 1] == "1234.500"
    assert args[args.index("-t") + 1] == "20.000"


def test_a_provider_with_no_transcriber_still_produces_frames(tmp_path: Path) -> None:
    def runner(tool, args, *, ok=(0,)):  # type: ignore[no-untyped-def]
        return None

    provider = SingleSeekEvidence(
        frames_dir=tmp_path / "frames", work_dir=tmp_path / "work", runner=runner
    )
    transcript, frame = provider.collect(Path("/srv/media/movies/example.mkv"), 1, 0.0)
    assert transcript == ""
    assert frame is not None


def test_the_wav_is_not_left_behind(tmp_path: Path) -> None:
    def runner(tool, args, *, ok=(0,)):  # type: ignore[no-untyped-def]
        (tmp_path / "mark0001.wav").write_bytes(b"")
        return None

    provider = SingleSeekEvidence(
        transcribe=lambda audio, language=None: "said", work_dir=tmp_path, runner=runner
    )
    provider.collect(Path("/srv/media/movies/example.mkv"), 1, 0.0)
    assert not (tmp_path / "mark0001.wav").exists()
