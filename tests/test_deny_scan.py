"""The gate has to fail when it should, and pass when it should.

A gate that has never failed is not known to work, so the first test here
plants findings and requires every one of them to be caught. The second
requires the repository itself to be clean, which is the assertion CI makes on
every pull request.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

from tests.deny_scan import canary, cast_exemptions, scan_text, scan_tree

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    assert [f.rule for f in found] == ["contact.email"]
    assert found[0].line == 2
    assert planted not in str(found[0])


def test_the_invented_cast_is_not_flagged() -> None:
    names = cast_exemptions()
    example = next((n for n in names if n.endswith(".mkv")), None)
    assert example is not None, "the cast must include a release-shaped example"
    assert scan_text(f"See {example} for the shape.\n", "example") == []
