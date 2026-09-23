"""The settle ladder, with no model, no GPU and no audio.

This is the highest-value test in the project, and the reason is not that the
ladder is complicated. It is that the ladder is the part that *decides*, the
part whose constants get adjusted, and the part whose mistakes are invisible:
a threshold that is slightly wrong does not crash, it proposes a few hundred
confident changes to somebody's files.

Hand-written probability vectors make all of it checkable in milliseconds. The
table lives in ``fixtures/posteriors.json`` with a sentence per case saying
what the case is *for*, so a failure names a rule rather than a row.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mkvkit.langid.ladder import (
    SettleBar,
    Support,
    Verdict,
    aggregate,
    mixed_split,
    settle,
    trimmed_aggregate,
)
from mkvkit.langid.priors import Prior
from mkvkit.langid.review import RescanReason, decide, rescan_queue
from mkvkit.langid.worker import TrackEvidence, WindowResult

CASES_FILE = Path(__file__).parent / "fixtures" / "posteriors.json"
CASES: list[dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _evidence(case: dict[str, Any]) -> TrackEvidence:
    windows = []
    for position, vector in enumerate(case["windows"]):
        counted = bool(vector.get("_counted", True))
        probabilities = {k: float(v) for k, v in vector.items() if not k.startswith("_")}
        windows.append(
            WindowResult(
                start_s=120.0 * (position + 1),
                probabilities=probabilities,
                speech_s=12.0 if counted else 0.0,
                counted=counted,
            )
        )
    return TrackEvidence(
        path=f"/srv/media/series/Northwind/case-{case['name'][:20]}",
        stream_index=1,
        model="fixture",
        windows=tuple(windows),
        existing_tag=case.get("tag"),
        runtime_s=case.get("runtime_s"),
    )


def _prior(case: dict[str, Any]) -> Prior | None:
    raw = case.get("prior")
    if raw is None:
        return None
    return Prior(
        language=raw.get("language"),
        strength=raw.get("strength"),
        sources=tuple(raw.get("sources", ())),
        blocked=tuple(raw.get("blocked", ())),
    )


def _support(case: dict[str, Any]) -> Support:
    raw = case.get("support") or {}
    return Support(
        text_confirmed=bool(raw.get("text_confirmed", False)),
        family_agreed=bool(raw.get("family_agreed", False)),
        speech_s=case.get("speech_total_s"),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_every_case_reaches_the_verdict_it_documents(case: dict[str, Any]) -> None:
    outcome = decide(_evidence(case), prior=_prior(case), support=_support(case))
    expected = case["expect"]
    assert outcome.decision.verdict.value == expected["verdict"], case["why"]
    if "language" in expected:
        assert outcome.decision.language == expected["language"]
    if "rule" in expected:
        assert outcome.decision.rule == expected["rule"]
    for reason in expected.get("reasons", ()):
        assert reason in outcome.reasons


def test_the_table_covers_every_verdict_the_ladder_can_reach() -> None:
    """A ladder with an untested rung is a ladder with an untested rung."""
    covered = {case["expect"]["verdict"] for case in CASES}
    assert covered == {verdict.value for verdict in Verdict}


def test_every_case_says_what_it_is_for() -> None:
    for case in CASES:
        assert case.get("why"), case["name"]


# ------------------------------------------------------------------ aggregation
def test_the_aggregate_is_a_geometric_mean_not_an_arithmetic_one() -> None:
    agreeing = [{"eng": 0.99, "deu": 0.01}] * 5
    assert aggregate(agreeing).conf > 0.98
    with_a_dissenter = [*agreeing, {"deu": 0.999, "eng": 0.001}]
    assert aggregate(with_a_dissenter).conf < 0.5
    # the arithmetic mean would still be above 0.82 here, which is the point
    arithmetic = sum(v.get("eng", 0.0) for v in with_a_dissenter) / len(with_a_dissenter)
    assert arithmetic > 0.82


def test_an_empty_aggregate_has_no_winner() -> None:
    assert aggregate([]).winner is None
    assert aggregate([]).counted == 0


def test_a_language_no_window_mentioned_does_not_win() -> None:
    post = aggregate([{"eng": 0.6, "deu": 0.4}, {"eng": 0.7, "fra": 0.3}])
    assert post.winner == "eng"


def test_trimming_cannot_change_who_the_winner_is() -> None:
    """The dropped windows are chosen by the untrimmed winner, deliberately.

    A trim that could also elect a different language would be a search for a
    language the evidence likes, which is the opposite of what it is for.
    """
    vectors = [{"eng": 0.99, "fra": 0.01}] * 8 + [{"fra": 0.98, "eng": 0.02}] * 2
    untrimmed = aggregate(vectors)
    trimmed = trimmed_aggregate(vectors, frac=0.2, min_counted=5)
    assert untrimmed.winner == trimmed.winner == "eng"
    assert trimmed.trimmed == 2
    assert trimmed.conf > untrimmed.conf


def test_trimming_does_nothing_when_there_is_too_little_to_trim() -> None:
    vectors = [{"eng": 0.9, "deu": 0.1}] * 3
    assert trimmed_aggregate(vectors, frac=0.2, min_counted=5).trimmed == 0


def test_a_split_needs_both_sides_to_be_substantial() -> None:
    bar = SettleBar()
    lopsided = [{"eng": 0.9, "deu": 0.1}] * 7 + [{"deu": 0.9, "eng": 0.1}] * 2
    assert mixed_split(lopsided, bar) is None
    even = [{"eng": 0.9, "deu": 0.1}] * 4 + [{"deu": 0.9, "eng": 0.1}] * 4
    assert mixed_split(even, bar) == ("eng", "deu")


# ------------------------------------------------------------- the bar as data
def test_every_threshold_is_configuration() -> None:
    """A tuned constant that is not configuration is a claimed universal."""
    strict = SettleBar(min_conf=0.999, min_counted=99)
    post = aggregate([{"eng": 0.99, "deu": 0.01}] * 6)
    assert settle(post, bar=strict).verdict is Verdict.UNSETTLED
    assert settle(post, bar=SettleBar()).verdict is Verdict.SETTLE


def test_the_override_bar_is_strictly_harder_than_the_standard_one() -> None:
    bar = SettleBar()
    assert bar.override_conf > bar.min_conf
    assert bar.override_counted > bar.min_counted
    assert bar.override_agree_frac > bar.min_agree_frac


def test_a_decision_names_the_rule_that_made_it() -> None:
    post = aggregate([{"eng": 0.99, "deu": 0.01}] * 6)
    decision = settle(post, bar=SettleBar())
    assert decision.rule
    assert ">=" in decision.reason  # the numbers it wanted, beside the ones it got


def test_only_some_verdicts_write() -> None:
    assert Verdict.SETTLE.writes and Verdict.OVERTURN.writes and Verdict.ZXX.writes
    assert not Verdict.CONFIRM.writes
    assert not Verdict.UNSETTLED.writes
    assert not Verdict.MIXED.writes


# ------------------------------------------------------------------- the queue
def test_the_queue_says_what_the_next_pass_should_do_differently() -> None:
    cases = {case["name"]: case for case in CASES}
    outcomes = [
        decide(_evidence(case), prior=_prior(case), support=_support(case))
        for case in cases.values()
    ]
    queued = rescan_queue(outcomes)
    reasons = {item.reason for item in queued}
    assert RescanReason.NEEDS_A_PERSON in reasons
    assert RescanReason.MORE_WINDOWS in reasons
    # cheapest first: a person is the last stage, never the first
    assert [item.stage for item in queued] == sorted(item.stage for item in queued)


def test_a_track_that_could_not_be_read_is_queued_as_a_read_failure() -> None:
    """Not as a content problem: nothing has been learned about the content."""
    evidence = TrackEvidence(
        path="/srv/media/series/Northwind/unreadable", stream_index=1,
        model="fixture", windows=(), error="ffmpeg exited 1", runtime_s=1800.0,
    )
    queued = rescan_queue([decide(evidence)])
    assert [item.reason for item in queued] == [RescanReason.READ_FAILED]
    assert queued[0].stage == 1


def test_a_track_of_pure_music_is_queued_for_different_windows() -> None:
    evidence = TrackEvidence(
        path="/srv/media/movies/The Quiet Harbour", stream_index=2, model="fixture",
        runtime_s=5400.0,
        windows=tuple(
            WindowResult(start_s=100.0 * n, probabilities={"eng": 0.3, "deu": 0.3},
                         speech_s=0.0, counted=False)
            for n in range(5)
        ),
    )
    queued = rescan_queue([decide(evidence)])
    assert [item.reason for item in queued] == [RescanReason.OTHER_WINDOWS]


def test_a_settled_track_is_never_queued() -> None:
    settled = [
        decide(_evidence(case), prior=_prior(case), support=_support(case))
        for case in CASES if case["expect"]["verdict"] != "unsettled"
    ]
    assert rescan_queue(settled) == []
