"""One survey shape, five formats, and the caveats that travel with it.

The test worth reading is the last one: every readable format has to carry the
caveats, because a survey is quoted back months later and by then the document
is the only thing left that can say what it counted.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jfkit.report import FORMATS, Column, Survey, render, write

STAMP = datetime(2026, 4, 3, 9, 15, 0, tzinfo=UTC)


def example() -> Survey:
    return Survey(
        name="Chapter state",
        about="What kind of chapter marks each item has, if any.",
        columns=[
            Column("title", "Title"),
            Column("marks", "Marks", kind="number"),
            Column("named", "Named", kind="bool"),
            Column("spacing", "Mean spacing (s)", kind="number", places=1),
        ],
        rows=[
            {"title": "The Quiet Harbour", "marks": 16, "named": True,
             "spacing": 337.25},
            {"title": "Blue Canyon", "marks": 0, "named": False, "spacing": None},
        ],
        summary={"items": 2, "with marks": 1},
        caveats=[
            "Counted from the catalogue, not from the files: an item whose marks "
            "have not been read yet counts as having none.",
        ],
        scope={"libraries": "Movies", "types": "Movie"},
        generated=STAMP,
    )


# ------------------------------------------------------------------- shape
def test_a_value_no_column_describes_is_refused() -> None:
    """A field nothing renders is a field nobody sees, so it is an error."""
    with pytest.raises(ValueError, match="which no column describes"):
        Survey(
            name="Example",
            columns=[Column("a", "A")],
            rows=[{"a": 1, "b": 2}],
        )


def test_the_stamp_is_the_only_thing_that_moves() -> None:
    first = render(example(), "tsv")
    second = render(example(), "tsv")
    assert first == second


def test_a_missing_value_is_empty_rather_than_the_word_none() -> None:
    assert "None" not in render(example(), "tsv")


# ----------------------------------------------------------------- formats
def test_the_tabular_formats_are_one_header_and_one_row_each() -> None:
    tsv = render(example(), "tsv").splitlines()
    assert tsv[0].split("\t") == ["Title", "Marks", "Named", "Mean spacing (s)"]
    assert tsv[1].split("\t") == ["The Quiet Harbour", "16", "yes", "337.2"]
    assert len(tsv) == 3

    csv_rows = render(example(), "csv").splitlines()
    assert csv_rows[0] == "Title,Marks,Named,Mean spacing (s)"


def test_the_machine_format_keeps_the_values_rather_than_their_rendering() -> None:
    payload = json.loads(render(example(), "json"))
    assert payload["rows"][0]["spacing"] == 337.25
    assert payload["rows"][1]["spacing"] is None
    assert payload["generated"] == "2026-04-03T09:15:00Z"
    assert payload["scope"] == {"libraries": "Movies", "types": "Movie"}


def test_the_readable_format_has_a_table_that_parses() -> None:
    lines = render(example(), "md").splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("| Title"))
    assert lines[header + 1].startswith("|---")
    assert lines[header + 2].count("|") == 5


def test_a_cell_containing_the_separator_does_not_end_the_row() -> None:
    survey = Survey(
        name="Example",
        columns=[Column("a", "A"), Column("b", "B")],
        rows=[{"a": "left | right", "b": "x"}],
        generated=STAMP,
    )
    row = next(line for line in render(survey, "md").splitlines() if "left" in line)
    assert row.count("|") - row.count("\\|") == 3
    assert "\\|" in row


def test_the_page_format_escapes_what_it_is_given() -> None:
    survey = Survey(
        name="Example",
        columns=[Column("a", "A")],
        rows=[{"a": "<script>not this</script>"}],
        generated=STAMP,
    )
    page = render(survey, "html")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(ValueError, match="not one of"):
        render(example(), "pdf")  # type: ignore[arg-type]


# ------------------------------------------------------------------ writing
def test_writing_produces_every_format_in_one_call(tmp_path: Path) -> None:
    written = write(example(), tmp_path)
    assert {p.suffix for p in written} == set(FORMATS.values())
    assert all(p.is_file() for p in written)
    assert {p.stem for p in written} == {"chapter-state"}


def test_writing_twice_produces_the_same_bytes(tmp_path: Path) -> None:
    """So two runs of the same survey diff to nothing, and a real change shows."""
    first = write(example(), tmp_path / "one")[0].read_bytes()
    second = write(example(), tmp_path / "two")[0].read_bytes()
    assert first == second


def test_an_unknown_format_is_refused_before_anything_is_written(
    tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not one of"):
        write(example(), tmp_path, formats=["tsv", "pdf"])


def test_every_readable_format_carries_the_caveats() -> None:
    """The rule the module exists for.

    A survey without its caveats is how a number measured on one collection
    becomes a claim about every collection, three documents later.
    """
    survey = example()
    marker = survey.caveats[0][:40]
    for fmt in ("md", "html", "json"):
        assert marker in render(survey, fmt), fmt


def test_a_survey_with_no_caveats_says_so_rather_than_saying_nothing() -> None:
    survey = Survey(name="Example", columns=[Column("a", "A")], generated=STAMP)
    assert "worth doubting" in render(survey, "md")
