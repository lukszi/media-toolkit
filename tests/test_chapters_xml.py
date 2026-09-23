"""Chapter documents: reading, writing, and the checks that block a bad one.

Nothing here needs a program or a media file. That is the point: a chapter
document is the artefact that gets applied to somebody's file, so the rules
that decide whether it may be applied have to be checkable in milliseconds.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import pytest
from mkvkit.chapters.xml import (
    Chapter,
    ChapterError,
    ChapterSet,
    build,
    downstream_label,
    format_timestamp,
    is_generic_name,
    parse,
    parse_document,
    parse_timestamp,
    rollback,
    selfcheck,
    trim_trailing,
)

SECOND = 1_000_000_000

DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Chapters SYSTEM "matroskachapters.dtd">
<Chapters>
  <EditionEntry>
    <EditionUID>4242</EditionUID>
    <EditionFlagDefault>1</EditionFlagDefault>
    <ChapterAtom>
      <ChapterUID>1</ChapterUID>
      <ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
      <ChapterFlagHidden>0</ChapterFlagHidden>
      <ChapterFlagEnabled>1</ChapterFlagEnabled>
      <ChapterDisplay>
        <ChapterString>The harbour at dawn</ChapterString>
        <ChapterLanguage>fre</ChapterLanguage>
      </ChapterDisplay>
    </ChapterAtom>
    <ChapterAtom>
      <ChapterUID>2</ChapterUID>
      <ChapterTimeStart>00:12:34.567891234</ChapterTimeStart>
      <ChapterFlagHidden>0</ChapterFlagHidden>
      <ChapterFlagEnabled>1</ChapterFlagEnabled>
    </ChapterAtom>
  </EditionEntry>
</Chapters>
"""


def grid(count: int = 12, step_s: float = 120.0) -> ChapterSet:
    return ChapterSet(
        tuple(
            Chapter(start_ns=int(i * step_s * SECOND), name=f"Mark {i + 1:02d}")
            for i in range(count)
        )
    )


# ---------------------------------------------------------------------- reading
def test_a_document_is_read_mark_for_mark() -> None:
    chapters = parse(DOCUMENT)
    assert len(chapters) == 2
    assert chapters.edition_uid == 4242
    assert chapters[0].name == "The harbour at dawn"
    assert chapters[0].start_ns == 0
    assert chapters[1].name is None
    assert chapters[1].uid == 2


def test_the_chapter_language_is_read_back_into_the_spelling_we_use() -> None:
    assert parse(DOCUMENT)[0].language == "fra"


def test_a_timestamp_keeps_every_digit() -> None:
    """Nanoseconds are the unit on disk; rounding them is a change to the file."""
    assert parse(DOCUMENT)[1].start_ns == 754_567_891_234
    assert format_timestamp(754_567_891_234) == "00:12:34.567891234"
    assert parse_timestamp(format_timestamp(1)) == 1


def test_something_that_is_not_a_timestamp_is_not_guessed_at() -> None:
    assert parse_timestamp("nonsense") is None
    assert parse_timestamp("12:34") is None
    assert parse_timestamp(None) is None


def test_a_document_with_two_editions_is_refused_rather_than_narrowed() -> None:
    """Applying one replaces them all, so picking one silently loses the rest."""
    two = DOCUMENT.replace(
        "</EditionEntry>",
        "</EditionEntry><EditionEntry><ChapterAtom>"
        "<ChapterTimeStart>00:00:01.000000000</ChapterTimeStart>"
        "</ChapterAtom></EditionEntry>",
        1,
    )
    assert len(parse_document(two)) == 2
    with pytest.raises(ChapterError, match=r"2 editions"):
        parse(two)


def test_a_mark_without_a_start_time_is_an_error() -> None:
    broken = DOCUMENT.replace("<ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>", "")
    with pytest.raises(ChapterError):
        parse(broken)


def test_a_document_that_does_not_parse_says_so() -> None:
    with pytest.raises(ChapterError, match="does not parse"):
        parse("<Chapters><EditionEntry>")


# ---------------------------------------------------------------------- writing
def test_what_is_written_reads_back_the_same() -> None:
    before = parse(DOCUMENT)
    after = parse(build(before))
    assert after.starts_ns == before.starts_ns
    assert after.names == before.names
    assert after[0].language == before[0].language
    assert build(after) == build(before)


def test_an_unnamed_mark_gets_no_display_block_at_all() -> None:
    """That is how "unnamed" is spelled. A generic label is not the same thing."""
    text = build(ChapterSet((Chapter(0), Chapter(SECOND, "A real name"))))
    assert text.count("<ChapterDisplay>") == 1
    assert "A real name" in text


def test_the_written_language_is_the_spelling_the_element_wants() -> None:
    text = build(ChapterSet((Chapter(0, "A name", language="fra"),)))
    assert "<ChapterLanguage>fre</ChapterLanguage>" in text
    assert "<ChapterLanguage>fra</ChapterLanguage>" not in text


def test_the_document_declares_itself() -> None:
    text = build(grid(2))
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "matroskachapters.dtd" in text
    assert text.count("<EditionEntry>") == 1
    assert "<EditionFlagDefault>1</EditionFlagDefault>" in text


def test_identifiers_are_left_out_unless_they_are_asked_for() -> None:
    before = parse(DOCUMENT)
    assert "<ChapterUID>" not in build(before)
    assert "<ChapterUID>1</ChapterUID>" in build(before, keep_uids=True)


def test_writing_is_stable() -> None:
    assert build(grid()) == build(grid())


# --------------------------------------------------------------------- rollback
def test_the_rollback_document_keeps_the_marks_and_drops_the_names() -> None:
    document = rollback(grid(4))
    restored = parse(document)
    assert restored.starts_ns == grid(4).starts_ns
    assert restored.names == (None, None, None, None)
    assert "<ChapterDisplay>" not in document


def test_renaming_never_touches_a_timestamp() -> None:
    before = grid(3)
    after = before.renamed(["One name", None, "Another"])
    assert after.starts_ns == before.starts_ns
    assert after.names == ("One name", None, "Another")


def test_a_name_list_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ChapterError, match="different cut"):
        grid(3).renamed(["only one"])


# ----------------------------------------------------------------- generic names
@pytest.mark.parametrize(
    "name",
    ["Chapter 7", "chapter 7", "Kapitel 03", "Scene 12", "Chapter One", "Teil 2",
     "12", "IV", "#3", "00:12:34", "1:02:03", "", "   ", "Chapter"],
)
def test_a_label_is_not_a_name(name: str) -> None:
    assert is_generic_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["The harbour at dawn", "Civil", "Seven", "Chapter and verse",
     "Part of the plan", "A scene in the rain", "Kapitelsberg"],
)
def test_a_name_is_not_a_label(name: str) -> None:
    assert is_generic_name(name) is False


def test_the_downstream_label_is_what_an_unnamed_mark_shows_as() -> None:
    assert downstream_label(7) == "Chapter 7"
    assert is_generic_name(downstream_label(7))


def test_a_set_counts_only_names_that_say_something() -> None:
    chapters = ChapterSet(
        (Chapter(0, "A real name"), Chapter(SECOND, "Chapter 2"), Chapter(2 * SECOND))
    )
    assert chapters.named_count == 1


# -------------------------------------------------------------------- self-check
def blocking(problems: list[object]) -> list[str]:
    return [p.rule for p in problems if getattr(p, "blocking", False)]  # type: ignore[attr-defined]


def notes(problems: list[object]) -> list[str]:
    return [p.rule for p in problems if not getattr(p, "blocking", True)]  # type: ignore[attr-defined]


def test_a_good_document_has_nothing_to_say() -> None:
    assert selfcheck(grid(12), runtime_s=1800.0) == []


def test_an_empty_document_is_refused() -> None:
    assert blocking(selfcheck(ChapterSet())) == ["empty"]


def test_marks_must_increase() -> None:
    chapters = ChapterSet((Chapter(2 * SECOND), Chapter(SECOND)))
    assert "order" in blocking(selfcheck(chapters))


def test_the_same_mark_twice_is_refused() -> None:
    chapters = ChapterSet((Chapter(SECOND), Chapter(SECOND)))
    assert "duplicate" in blocking(selfcheck(chapters))


def test_a_hidden_mark_is_refused() -> None:
    chapters = ChapterSet((Chapter(0), Chapter(SECOND, hidden=True)))
    assert "hidden" in blocking(selfcheck(chapters))


def test_a_mark_past_the_runtime_is_refused_and_one_near_the_end_is_noted() -> None:
    late = ChapterSet((Chapter(0), Chapter(120 * SECOND)))
    assert "past-the-end" in blocking(selfcheck(late, runtime_s=100.0))
    assert "near-the-end" in notes(selfcheck(late, runtime_s=125.0))


def test_a_late_first_mark_is_a_note_not_a_rejection() -> None:
    chapters = ChapterSet((Chapter(60 * SECOND), Chapter(120 * SECOND)))
    problems = selfcheck(chapters, runtime_s=3600.0)
    assert blocking(problems) == []
    assert "first-mark" in notes(problems)


def test_a_count_that_does_not_match_the_file_is_refused() -> None:
    problems = selfcheck(grid(4), runtime_s=3600.0, file_count=16)
    assert "file-count" in blocking(problems)
    assert "lose 12" in str(problems[0])


def test_a_count_that_does_not_match_the_source_is_refused() -> None:
    assert "count" in blocking(selfcheck(grid(4), expected_count=5))


def test_a_name_with_an_unrecoverable_byte_is_refused() -> None:
    """The original character cannot be recovered, so the name is not usable."""
    chapters = ChapterSet((Chapter(0, "The h�rbour"),))
    assert "encoding" in blocking(selfcheck(chapters))


def test_a_document_that_is_labels_all_the_way_down_is_refused() -> None:
    chapters = ChapterSet(
        tuple(Chapter(i * SECOND, f"Chapter {i + 1}") for i in range(6))
    )
    assert "not-names" in blocking(selfcheck(chapters))


def test_a_document_with_no_names_at_all_is_fine() -> None:
    chapters = ChapterSet(tuple(Chapter(i * SECOND) for i in range(6)))
    assert selfcheck(chapters) == []


# ---------------------------------------------------------------------- trimming
def test_trailing_marks_past_the_end_are_trimmed_and_reported() -> None:
    """An exported list routinely carries the end of the disc as a mark."""
    chapters = ChapterSet(
        (Chapter(0), Chapter(60 * SECOND), Chapter(3600 * SECOND))
    )
    kept, dropped = trim_trailing(chapters, runtime_s=3000.0)
    assert len(kept) == 2
    assert len(dropped) == 1
    assert selfcheck(kept, runtime_s=3000.0) == []


# ------------------------------------------------------------- against the files
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_marks_of_a_file_are_read_back_as_a_document(
    media_fixtures: dict[str, object],
) -> None:
    from mkvkit.chapters.xml import read_chapters

    from tests.fixtures import CHAPTER_NAMES, CHAPTER_TIMES_S, GRID_DURATION_S

    chapters = read_chapters(media_fixtures["chapter_grid.mkv"])  # type: ignore[arg-type]
    assert len(chapters) == len(CHAPTER_TIMES_S)
    assert chapters.names == CHAPTER_NAMES
    assert chapters.starts_s[1] == pytest.approx(CHAPTER_TIMES_S[1], abs=0.005)
    # The fixture is short, so its last mark is close to the end -- which is a
    # note about a small file and not a reason to refuse anything.
    problems = selfcheck(chapters, runtime_s=GRID_DURATION_S)
    assert blocking(problems) == []
    assert notes(problems) == ["near-the-end"]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_file_with_no_marks_reads_as_an_empty_set(
    media_fixtures: dict[str, object],
) -> None:
    from mkvkit.chapters.xml import read_chapters

    assert len(read_chapters(media_fixtures["tiny_multitrack.mkv"])) == 0  # type: ignore[arg-type]
