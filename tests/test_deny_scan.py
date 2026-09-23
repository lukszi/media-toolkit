"""The gate has to fail when it should, and pass when it should.

A gate that has never failed is not known to work, so the first test here
plants findings and requires every one of them to be caught. The second
requires the repository itself to be clean, which is the assertion CI makes on
every pull request.

The rest are about the table itself. It is written from one source rather than
typed twice, and these check the properties that make that worth doing: the
markers are still there, the ids are unique, and nothing in the file is an
example of what the file looks for.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

from tests.deny_scan import RULES, canary, cast_exemptions, scan_text, scan_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "tests" / "deny_scan.py"


def test_the_gate_catches_every_planted_finding() -> None:
    assert canary() == 0


def test_the_repository_is_clean() -> None:
    findings = scan_tree(REPO_ROOT)
    assert findings == [], [str(f) for f in findings]


def test_the_cast_comes_from_the_conventions_file() -> None:
    names = cast_exemptions()
    assert names, "the invented cast must be readable from its own document"
    assert any("Quiet" in name for name in names)


def test_a_finding_never_prints_what_it_matched() -> None:
    planted = "someone" + "@" + "not-an-example.test"
    found = scan_text(f"line one\n{planted}\n", "example", exemptions=())
    assert [f.rule for f in found] == ["email"]
    assert found[0].line == 2
    assert planted not in str(found[0])


def test_the_invented_cast_is_not_flagged() -> None:
    names = cast_exemptions()
    example = next((n for n in names if n.endswith(".mkv")), None)
    assert example is not None, "the cast must include a release-shaped example"
    assert scan_text(f"See {example} for the shape.\n", "example") == []


# ------------------------------------------------------------------ the table
def test_the_table_is_still_written_rather_than_typed() -> None:
    """The markers are what a writer rewrites; losing them loses the guarantee."""
    text = GATE.read_text(encoding="utf-8")
    assert "do not edit by hand" in text
    assert "end of the written rule table" in text


def test_every_rule_has_a_unique_id_and_a_reason() -> None:
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert all(rule.why for rule in RULES)


def test_the_gate_does_not_flag_itself() -> None:
    """The one file guaranteed to contain something like every pattern."""
    relative = GATE.relative_to(REPO_ROOT).as_posix()
    findings = scan_text(
        GATE.read_text(encoding="utf-8"), relative, path=relative, exemptions=()
    )
    assert findings == [], [str(f) for f in findings]
