"""The report: what it leads with, and what it refuses to claim.

A report about other people's files is the artefact somebody approves, so its
properties are requirements rather than taste. It leads with what did not
settle; it says which rule decided each verdict; it labels its own numbers as
a measurement of one collection; and it is byte-stable, so two runs can be
diffed and the diff is the news.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from datetime import UTC, datetime

from mkvkit.langid.ladder import Verdict
from mkvkit.langid.report import TSV_COLUMNS, render_markdown, render_tsv, summarise
from mkvkit.langid.review import Outcome, decide, rescan_queue
from mkvkit.langid.worker import TrackEvidence, WindowResult

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _evidence(
    name: str, vector: dict[str, float], *, windows: int = 6, tag: str | None = None
) -> TrackEvidence:
    return TrackEvidence(
        path=f"/srv/media/series/Northwind/{name}",
        stream_index=1,
        model="fixture",
        existing_tag=tag,
        runtime_s=2700.0,
        windows=tuple(
            WindowResult(start_s=120.0 * (n + 1), probabilities=dict(vector), speech_s=14.0)
            for n in range(windows)
        ),
    )


def _outcomes() -> list[Outcome]:
    return [
        decide(_evidence("a", {"eng": 0.99, "deu": 0.01})),
        decide(_evidence("b", {"eng": 0.99, "deu": 0.01}, tag="eng")),
        decide(_evidence("c", {"eng": 0.70, "deu": 0.30}, windows=3)),
        decide(_evidence("d", {"eng": 0.95, "deu": 0.05}, tag="deu")),
    ]


def test_the_report_leads_with_what_did_not_settle() -> None:
    """The queue is the only part that needs a decision from a person."""
    outcomes = _outcomes()
    text = render_markdown(outcomes, rescan_queue(outcomes), generated=WHEN)
    assert text.index("## What did not settle") < text.index("## Verdicts")


def test_the_report_says_which_rule_decided() -> None:
    outcomes = _outcomes()
    text = render_markdown(outcomes, generated=WHEN)
    assert "## Which rule decided" in text
    assert "A.tag-confirm" in text


def test_the_report_labels_its_own_numbers_as_one_collection_s() -> None:
    """An agreement rate is a statement about a library, not about a detector."""
    outcomes = _outcomes()
    text = render_markdown(outcomes, generated=WHEN)
    assert "not transferable" in text
    assert "defaults fitted against one library" in text or "fitted" in text


def test_the_report_is_byte_stable() -> None:
    outcomes = _outcomes()
    first = render_markdown(outcomes, rescan_queue(outcomes), generated=WHEN)
    second = render_markdown(outcomes, rescan_queue(outcomes), generated=WHEN)
    assert first == second


def test_a_run_that_settled_everything_says_so() -> None:
    outcomes = [decide(_evidence("a", {"eng": 0.99, "deu": 0.01}))]
    text = render_markdown(outcomes, [], generated=WHEN)
    assert "every track reached a verdict" in text


def test_the_summary_counts_writes_rather_than_verdicts() -> None:
    summary = summarise(_outcomes())
    assert summary.total == 4
    # one settle writes; a confirm does not, and neither does an unsettled track
    assert summary.writes == 1
    assert summary.verdicts[Verdict.CONFIRM.value] == 1


def test_agreement_is_measured_only_over_tracks_that_had_a_tag() -> None:
    summary = summarise(_outcomes())
    assert summary.tagged == 2
    assert summary.agreement == 0.5


def test_agreement_over_nothing_is_not_a_number() -> None:
    summary = summarise([decide(_evidence("a", {"eng": 0.99}))])
    assert summary.agreement is None


def test_the_tsv_has_one_row_per_track_in_a_stable_order() -> None:
    rows = render_tsv(_outcomes()).splitlines()
    assert rows[0].split("\t") == list(TSV_COLUMNS)
    assert len(rows) == 5
    paths = [row.split("\t")[0] for row in rows[1:]]
    assert paths == sorted(paths)


def test_the_tsv_survives_a_reason_containing_a_tab() -> None:
    outcomes = _outcomes()
    for row in render_tsv(outcomes).splitlines():
        assert len(row.split("\t")) == len(TSV_COLUMNS)
