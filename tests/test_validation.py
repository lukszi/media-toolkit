"""The warning a change owes before it costs the server a whole-library validation.

A folder directly under a library root that appears or disappears makes the
server's watcher refresh the library's own collection folder, which is a
validation of everything below the root. These tests pin which changes say
so beforehand and which do not.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from jfkit.validation import expect_library_validation, warnings_for

ROOTS = ["/srv/media/series", "/srv/media/movies"]


def test_removing_a_top_level_folder_warns_of_a_library_validation() -> None:
    found = expect_library_validation(roots=ROOTS, removes=["/srv/media/series/Northwind"])
    assert [(w.change, w.root) for w in found] == [("removes", "/srv/media/series")]
    assert "validate that whole library" in str(found[0])


def test_removing_something_deeper_warns_of_nothing() -> None:
    assert expect_library_validation(
        roots=ROOTS, removes=["/srv/media/series/Northwind/Season 1/Northwind - S01E03.mkv"]
    ) == []


def test_removing_a_root_or_its_parent_is_the_loudest_warning() -> None:
    found = expect_library_validation(roots=ROOTS, removes=["/srv/media"])
    assert {w.change for w in found} == {"removes a root"}
    assert len(found) == 2


def test_a_move_into_a_new_top_level_folder_warns_and_into_an_existing_one_does_not() -> None:
    existing = {"/srv/media/movies/Blue Canyon (1998)"}
    found = expect_library_validation(
        roots=ROOTS,
        creates=["/srv/media/movies/Winter Tide (2011)/Winter Tide (2011).mkv",
                 "/srv/media/movies/Blue Canyon (1998)/Blue Canyon (1998).mkv"],
        exists=lambda p: p in existing,
    )
    assert [(w.change, w.path) for w in found] == [
        ("creates", "/srv/media/movies/Winter Tide (2011)")
    ]


def test_either_separator_and_any_case_match_a_root() -> None:
    found = expect_library_validation(
        roots=["C:/Media/Series"], removes=["c:\\media\\series\\Northwind"]
    )
    assert len(found) == 1


def test_roots_that_could_not_be_read_are_said_to_be_unknown() -> None:
    assert "unknown" in warnings_for(None, removes=["/srv/media/series/Northwind"])[0]
    assert warnings_for(ROOTS, removes=["/srv/media/series/Northwind"])


