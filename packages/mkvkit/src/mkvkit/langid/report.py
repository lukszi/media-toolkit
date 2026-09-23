"""mkvkit.langid.report -- what the pass found, in a form somebody will read.

A language pass proposes changes to other people's files. The report is not a
courtesy at the end of it: it is the artefact a person approves or rejects,
and everything about it follows from that.

**It says what it did not settle, first.** A report that leads with its
successes is a report that buries the queue, and the queue is the only part
that needs a decision.

**Every number carries its scope.** The counts here describe the run that
produced them, not the tool: an agreement rate, however high, is a
statement about one collection's tagging, and quoting it without the
collection is how a measurement becomes a claim.

**It never prints a threshold without the rule that used it.** "Unsettled,
confidence 0.88" tells a reader nothing; "unsettled: standard bar wanted 0.92"
tells them what to change.

Two renderings, one shape: a markdown summary for a person and a TSV for
whatever comes next. Both are deterministic -- same input, same bytes -- so a
report can be diffed against the previous run, which is where the interesting
changes show up.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime

from ..langcodes import canonical
from .ladder import SettleBar, Verdict
from .review import Outcome, Rescan

__all__ = ["TSV_COLUMNS", "Summary", "render_markdown", "render_tsv", "summarise"]

TSV_COLUMNS = (
    "path", "stream_index", "existing_tag", "detected", "verdict", "rule",
    "confidence", "margin", "agreement", "counted_windows", "reason",
)


class Summary:
    """The counts a report is built from, computed once."""

    def __init__(self, outcomes: Sequence[Outcome], bar: SettleBar | None = None) -> None:
        self.bar = bar or SettleBar()
        self.outcomes = list(outcomes)
        self.verdicts: Counter[str] = Counter(
            outcome.decision.verdict.value for outcome in self.outcomes
        )
        self.rules: Counter[str] = Counter(
            outcome.decision.rule for outcome in self.outcomes
        )
        self.reasons: Counter[str] = Counter(
            reason for outcome in self.outcomes for reason in outcome.reasons
        )
        self.languages: Counter[str] = Counter(
            outcome.decision.language for outcome in self.outcomes
            if outcome.decision.language
        )
        tagged = [o for o in self.outcomes if canonical(o.evidence.existing_tag)]
        self.tagged = len(tagged)
        self.agreed = sum(
            1 for o in tagged
            if o.decision.posterior.winner == canonical(o.evidence.existing_tag)
        )

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def writes(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.writes)

    @property
    def agreement(self) -> float | None:
        """How often the audio agreed with a tag that was already there.

        A property of the collection at least as much as of the detector: a
        library tagged carefully by one person and one tagged by whatever
        muxed the file do not produce the same number, and neither number says
        anything about the other library.
        """
        return self.agreed / self.tagged if self.tagged else None


def summarise(outcomes: Iterable[Outcome], bar: SettleBar | None = None) -> Summary:
    return Summary(list(outcomes), bar)


def render_tsv(outcomes: Sequence[Outcome]) -> str:
    """One row per track, tab separated, stable order.

    Tab separated rather than comma separated because these rows carry file
    names, and a file name containing a comma is ordinary while one containing
    a tab is not.
    """
    lines = ["\t".join(TSV_COLUMNS)]
    for outcome in sorted(
        outcomes, key=lambda o: (o.evidence.path, o.evidence.stream_index)
    ):
        posterior = outcome.decision.posterior
        lines.append("\t".join(str(cell) for cell in (
            outcome.evidence.path,
            outcome.evidence.stream_index,
            outcome.evidence.existing_tag or "",
            outcome.decision.language or posterior.winner or "",
            outcome.decision.verdict.value,
            outcome.decision.rule,
            f"{posterior.conf:.4f}",
            f"{posterior.margin:.4f}",
            f"{posterior.agree_frac:.2f}",
            posterior.counted,
            outcome.decision.reason.replace("\t", " "),
        )))
    return "\n".join(lines) + "\n"


def render_markdown(
    outcomes: Sequence[Outcome],
    queue: Sequence[Rescan] = (),
    *,
    bar: SettleBar | None = None,
    generated: datetime | None = None,
    scope: str = "",
) -> str:
    """The summary a person reads before approving anything."""
    summary = summarise(outcomes, bar)
    stamp = (generated or datetime.now().astimezone()).isoformat(timespec="seconds")
    out: list[str] = [
        "# Spoken-language pass",
        "",
        f"Generated {stamp}." + (f" Scope: {scope}." if scope else ""),
        "",
        f"{summary.total} track(s) examined, {summary.writes} would be written.",
        "",
        "Every number below describes this run over this collection. The",
        "thresholds are fitted defaults, not universals; see",
        "the method document before reading any of them as a property of the",
        "detector.",
        "",
        "## What did not settle",
        "",
    ]
    if not queue:
        out += ["Nothing: every track reached a verdict.", ""]
    else:
        out += [
            f"{len(queue)} track(s), grouped by what the next pass should do",
            "differently. Re-sampling is cheap and settles most of what is left;",
            "a person is the last stage because their time is the scarcest thing",
            "here.",
            "",
            "| reason | stage | tracks |",
            "|---|---:|---:|",
        ]
        by_reason = Counter(item.reason for item in queue)
        stages = {item.reason: item.stage for item in queue}
        for reason, count in sorted(by_reason.items(), key=lambda kv: (stages[kv[0]], kv[0])):
            out.append(f"| {reason} | {stages[reason]} | {count} |")
        out.append("")

    out += ["## Verdicts", "", "| verdict | tracks | writes |", "|---|---:|---|"]
    for verdict in Verdict:
        count = summary.verdicts.get(verdict.value, 0)
        if count:
            out.append(
                f"| {verdict.value} | {count} | {'yes' if verdict.writes else 'no'} |"
            )
    out.append("")

    out += ["## Which rule decided", "", "| rule | tracks |", "|---|---:|"]
    for rule, count in sorted(summary.rules.items(), key=lambda kv: (-kv[1], kv[0])):
        if rule:
            out.append(f"| `{rule}` | {count} |")
    out.append("")

    if summary.tagged:
        agreement = summary.agreement or 0.0
        out += [
            "## Agreement with the tags that were already there",
            "",
            f"{summary.agreed} of {summary.tagged} tagged track(s), "
            f"{agreement * 100:.1f} per cent.",
            "",
            "This is a measurement of the collection's existing tagging as much",
            "as of the detector. It is not transferable.",
            "",
        ]

    if summary.reasons:
        out += ["## Why tracks did not settle", "", "| reason | tracks |", "|---|---:|"]
        for reason, count in sorted(summary.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"| {reason} | {count} |")
        out.append("")

    out += [
        "## Caveats",
        "",
        "- The thresholds are fitted defaults, not universals.",
        "- A confirmed tag writes nothing; only a settle, an overturn or a",
        "  no-speech verdict proposes a change.",
        "- A mixed verdict is a conclusion, not a failure: a track that is",
        "  genuinely two languages cannot be described by one tag.",
        "",
    ]
    return "\n".join(out)
