"""Library options: the fields the document leaves out, and the write that proves itself.

The two tests worth reading are the one that shows what a round-trip does to
an omitted field when the defaults table is not applied first, and the one
where a library is listed with no identifier and no options because its
stored path is stale -- which is the symptom everybody attributes to the
plugin that reports it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from jfkit.libopts import (
    CTOR_DEFAULTS,
    backup_options,
    libraries,
    options_document,
    parse_options,
    patch,
    read_options,
    root_path_problems,
    toggled,
    write_options,
)

from tests.fake_server import (
    Recorder,
    client_for,
    fake_server,
    write_options_document,
)

LIBRARY_ID = "00000000-0000-0000-0000-000000000101"

#: A document of the shape the server writes: only what differs from the
#: defaults, and nothing else.
SPARSE = """<LibraryOptions>
  <EnableEmbeddedTitles>true</EnableEmbeddedTitles>
  <PreferredMetadataLanguage>en</PreferredMetadataLanguage>
  <MetadataSavers><string>Nfo</string></MetadataSavers>
  <AutomaticRefreshIntervalDays>0</AutomaticRefreshIntervalDays>
</LibraryOptions>
"""


@pytest.fixture
def server() -> Iterator[tuple[str, Recorder]]:
    with fake_server() as running:
        yield running


@pytest.fixture
def library(tmp_path: Path) -> Path:
    folder = tmp_path / "Movies"
    folder.mkdir()
    document = options_document(folder)
    document.write_text(SPARSE, encoding="utf-8", newline="\n")
    return document


# ------------------------------------------------------------------ reading
def test_a_document_only_carries_what_differs_from_the_defaults(
    library: Path
) -> None:
    values, named, unknown = parse_options(SPARSE)
    assert named == [
        "EnableEmbeddedTitles", "PreferredMetadataLanguage", "MetadataSavers",
        "AutomaticRefreshIntervalDays",
    ]
    assert not unknown
    assert len(values) == len(CTOR_DEFAULTS)


def test_the_omitted_fields_come_from_the_table_not_from_nothing(
    library: Path
) -> None:
    """The reason the table exists.

    Four fields are in the document. The rest are not missing, they are at
    their defaults -- and an object rebuilt without them says the library
    should stop saving subtitles beside the media and start grouping nothing.
    """
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    assert options["EnableEmbeddedTitles"] is True
    assert options["SaveSubtitlesWithMedia"] is True          # a default, not stated
    assert options["EnableAutomaticSeriesGrouping"] is True   # likewise
    assert options["SeasonZeroDisplayName"] == "Specials"
    assert "SaveSubtitlesWithMedia" in options.defaulted
    assert "EnableEmbeddedTitles" not in options.defaulted


def test_an_element_the_table_does_not_know_is_reported_not_dropped(
    tmp_path: Path
) -> None:
    document = tmp_path / "options.xml"
    document.write_text(
        "<LibraryOptions><SomethingNewer>true</SomethingNewer></LibraryOptions>",
        encoding="utf-8",
    )
    options = read_options(document, library_id=LIBRARY_ID, name="Movies")
    assert options.unknown == ("SomethingNewer",)


def test_a_write_is_refused_while_an_unknown_element_is_present(
    server: tuple[str, Recorder], tmp_path: Path
) -> None:
    """Rather than round-tripping the field into oblivion."""
    url, _ = server
    document = tmp_path / "options.xml"
    document.write_text(
        "<LibraryOptions><SomethingNewer>true</SomethingNewer></LibraryOptions>",
        encoding="utf-8",
    )
    options = read_options(document, library_id=LIBRARY_ID, name="Movies")
    with pytest.raises(ValueError, match="does not know"):
        write_options(client_for(url), options, {"EnableEmbeddedTitles": False})


# ------------------------------------------------------------------ patching
def test_patching_reports_exactly_what_moved(library: Path) -> None:
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    new, moved = patch(options, {"EnableEmbeddedTitles": False})
    assert moved == ["EnableEmbeddedTitles"]
    assert new["PreferredMetadataLanguage"] == "en"


def test_patching_a_field_that_is_not_an_option_is_refused(library: Path) -> None:
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    with pytest.raises(KeyError, match="not a library option"):
        patch(options, {"EnableEmbededTitles": False})


# ------------------------------------------------------------------ writing
def test_a_write_reads_the_document_back_and_checks_what_changed(
    server: tuple[str, Recorder], library: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")

    result = write_options(client, options, {"EnableEmbeddedTitles": False})

    assert result.applied
    assert result.ok, str(result)
    assert result.moved == ("EnableEmbeddedTitles",)
    assert read_options(library)["EnableEmbeddedTitles"] is False


def test_a_second_field_moving_is_a_problem_and_not_a_shrug(
    server: tuple[str, Recorder], library: Path
) -> None:
    """The check that makes the defaults table falsifiable.

    Here the server writes back an extra change nobody asked for. The write
    says so, instead of reporting success because the field it cared about
    came out right.
    """
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")

    result = write_options(
        client, options,
        {"EnableEmbeddedTitles": False, "SaveSubtitlesWithMedia": False},
        expect=["EnableEmbeddedTitles"],
    )
    assert not result.ok
    assert "nobody asked about" in str(result)


def test_a_dry_run_write_sends_nothing(
    server: tuple[str, Recorder], library: Path
) -> None:
    url, recorder = server
    client = client_for(url)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    result = write_options(client, options, {"EnableEmbeddedTitles": False})
    assert not result.applied
    assert recorder.options_writes == []
    assert read_options(library)["EnableEmbeddedTitles"] is True


def test_a_write_leaves_the_document_it_replaced_on_disk(
    server: tuple[str, Recorder], library: Path, tmp_path: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    result = write_options(
        client, options, {"EnableEmbeddedTitles": False},
        backup_dir=tmp_path / "backups",
    )
    assert result.backup is not None and result.backup.is_file()
    assert "<EnableEmbeddedTitles>true" in result.backup.read_text(encoding="utf-8")


def test_a_library_with_no_identifier_cannot_be_written(
    server: tuple[str, Recorder], library: Path
) -> None:
    url, _ = server
    options = read_options(library, name="Movies")
    with pytest.raises(ValueError, match="stale"):
        write_options(client_for(url), options, {"EnableEmbeddedTitles": False})


def test_a_backup_is_written_without_a_write(library: Path, tmp_path: Path) -> None:
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    kept = backup_options(options, tmp_path / "backups")
    assert kept is not None
    assert (tmp_path / "backups").glob("Movies.*.json")


# ------------------------------------------------------------------ the toggle
def test_an_option_turned_on_for_one_pass_is_put_back_afterwards(
    server: tuple[str, Recorder], library: Path
) -> None:
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    field = "ExtractChapterImagesDuringLibraryScan"

    with toggled(client, options, field, True):
        assert read_options(library)[field] is True
    assert read_options(library)[field] is False


def test_the_option_goes_back_even_when_the_pass_fails(
    server: tuple[str, Recorder], library: Path
) -> None:
    """The failure that matters: a pass that stops halfway leaves it on."""
    url, recorder = server
    client = client_for(url, dry_run=False)
    recorder.options_documents[LIBRARY_ID] = library
    options = read_options(library, library_id=LIBRARY_ID, name="Movies")
    field = "ExtractChapterImagesDuringLibraryScan"

    with pytest.raises(RuntimeError), toggled(client, options, field, True):
        raise RuntimeError("the pass fell over")
    assert read_options(library)[field] is False


# ------------------------------------------------------------- the root check
def test_a_library_listed_with_nothing_in_it_is_a_stale_path(
    server: tuple[str, Recorder]
) -> None:
    """The finding, and the two-line check that names it.

    The route answers. It answers with an identifier of nothing and options
    of nothing, for every library, because it matches records by comparing
    paths exactly and the data directory moved. Everything downstream then
    reports that the library is invalid.
    """
    url, recorder = server
    recorder.virtual_folders = [
        {"Name": "Movies", "ItemId": None, "LibraryOptions": None,
         "Path": "/var/lib/old-location/root/default/Movies"},
        {"Name": "Series", "ItemId": None, "LibraryOptions": None,
         "Path": "/var/lib/old-location/root/default/Series"},
    ]
    client = client_for(url)
    found = root_path_problems(client, data_dir="/var/lib/media-server")
    assert len(found) == 2
    assert "not under" in str(found[0])


def test_a_library_that_resolves_is_not_a_problem(
    server: tuple[str, Recorder]
) -> None:
    url, recorder = server
    recorder.virtual_folders = [
        {"Name": "Movies", "ItemId": LIBRARY_ID,
         "LibraryOptions": {"EnableEmbeddedTitles": False},
         "Path": "/var/lib/media-server/root/default/Movies"},
    ]
    client = client_for(url)
    assert root_path_problems(client, data_dir="/var/lib/media-server") == []
    assert len(libraries(client)) == 1


def test_the_stand_in_writes_a_document_the_parser_reads(tmp_path: Path) -> None:
    document = tmp_path / "options.xml"
    write_options_document(document, dict(CTOR_DEFAULTS))
    values, _named, unknown = parse_options(document.read_text(encoding="utf-8"))
    assert not unknown
    assert values["SeasonZeroDisplayName"] == "Specials"
