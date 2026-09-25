"""The tag element: reading it, merging into it, and the comparison that works.

Most of this is a document in and a document out, so it runs anywhere. The
two at the end read the one fixture that carries a language tag contradicting
its own track header, because that file cannot be produced by the encoder
alone.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from mkvkit.tags import (
    SimpleTag,
    Tag,
    TagError,
    TagSet,
    Targets,
    build,
    merge,
    parse,
    provenance,
    read_tags,
    set_track_language,
)

TRACK_UID = 111222333
OTHER_UID = 444555666

DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Tags SYSTEM "matroskatags.dtd">
<Tags>
  <Tag>
    <Targets/>
    <Simple><Name>ENCODER</Name><String>an encoder</String></Simple>
  </Tag>
  <Tag>
    <Targets>
      <TargetTypeValue>50</TargetTypeValue>
      <TrackUID>{TRACK_UID}</TrackUID>
    </Targets>
    <Simple><Name>LANGUAGE</Name><String>fra</String></Simple>
    <Simple><Name>DURATION</Name><String>00:00:05.000000000</String></Simple>
  </Tag>
  <Tag>
    <Targets><TrackUID>{OTHER_UID}</TrackUID></Targets>
    <Simple><Name>DURATION</Name><String>00:00:05.000000000</String></Simple>
  </Tag>
</Tags>
"""

#: The same content after the editor has rewritten it: the default target type
#: value is gone, an explicit target type has appeared, and every simple tag
#: has gained a language element. Nothing about the file has changed.
NORMALISED = f"""<?xml version="1.0"?>
<Tags>
  <Tag>
    <Targets/>
    <Simple>
      <Name>ENCODER</Name><String>an encoder</String>
      <TagLanguageIETF>und</TagLanguageIETF>
    </Simple>
  </Tag>
  <Tag>
    <Targets><TargetType>MOVIE</TargetType><TrackUID>{TRACK_UID}</TrackUID></Targets>
    <Simple>
      <Name>LANGUAGE</Name><String>fra</String><TagLanguage>und</TagLanguage>
    </Simple>
    <Simple>
      <Name>DURATION</Name><String>00:00:05.000000000</String>
    </Simple>
  </Tag>
  <Tag>
    <Targets><TrackUID>{OTHER_UID}</TrackUID></Targets>
    <Simple>
      <Name>DURATION</Name><String>00:00:05.000000000</String>
    </Simple>
  </Tag>
</Tags>
"""


# ---------------------------------------------------------------------- reading
def test_a_document_is_read_tag_for_tag() -> None:
    tags = parse(DOCUMENT)
    assert len(tags) == 3
    assert tags.tags[0].targets.is_global
    assert tags.tags[1].targets.track_uids == (TRACK_UID,)
    assert tags.tags[1].targets.type_value == 50


def test_no_tags_at_all_is_an_empty_set_not_an_error() -> None:
    assert parse("") == TagSet()
    assert parse(None) == TagSet()
    assert not parse("")


def test_a_document_that_does_not_parse_says_so() -> None:
    with pytest.raises(TagError, match="does not parse"):
        parse("<Tags><Tag>")


def test_a_byte_order_mark_does_not_stop_it() -> None:
    """The extractor writes one; a parser that does not expect it fails on every file."""
    assert len(parse("﻿" + DOCUMENT)) == 3


def test_the_language_a_tag_claims_is_found_by_track(
) -> None:
    tags = parse(DOCUMENT)
    assert tags.language_of(TRACK_UID) == "fra"
    assert tags.language_of(OTHER_UID) is None
    assert tags.tracks_with_language() == {TRACK_UID: "fra"}


def test_a_language_tag_is_canonicalised_like_every_other_code() -> None:
    document = DOCUMENT.replace("<String>fra</String>", "<String>fre</String>")
    assert parse(document).language_of(TRACK_UID) == "fra"


# ------------------------------------------------------------------ comparison
def test_the_comparison_is_blind_to_the_editor_s_normalisation() -> None:
    """Otherwise every rewritten tag reads as lost and re-added, every time."""
    assert parse(DOCUMENT).triples == parse(NORMALISED).triples


def test_the_comparison_still_sees_a_tag_that_really_went_missing() -> None:
    fewer = NORMALISED.replace(
        "<Name>LANGUAGE</Name><String>fra</String><TagLanguage>und</TagLanguage>",
        "<Name>LANGUAGE</Name><String>fra</String>",
    ).replace("<Name>ENCODER</Name><String>an encoder</String>", "<Name>X</Name><String/>")
    assert parse(DOCUMENT).triples != parse(fewer).triples


def test_the_comparison_sees_a_value_that_changed() -> None:
    changed = DOCUMENT.replace("<String>fra</String>", "<String>deu</String>")
    assert parse(DOCUMENT).triples != parse(changed).triples


# ---------------------------------------------------------------------- writing
def test_what_is_written_reads_back_the_same() -> None:
    before = parse(DOCUMENT)
    assert parse(build(before)).triples == before.triples


def test_the_document_declares_itself() -> None:
    text = build(parse(DOCUMENT))
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "matroskatags.dtd" in text
    assert f"<TrackUID>{TRACK_UID}</TrackUID>" in text


def test_writing_is_stable() -> None:
    tags = parse(DOCUMENT)
    assert build(tags) == build(tags)


# ----------------------------------------------------------- the language rewrite
def test_a_language_tag_is_rewritten_to_agree_with_the_header() -> None:
    tags = set_track_language(parse(DOCUMENT), TRACK_UID, "deu")
    assert tags.language_of(TRACK_UID) == "deu"
    # and nothing else moved
    assert len(tags) == 3
    assert ("", "ENCODER", "an encoder") in tags.triples


def test_a_track_with_no_language_tag_gains_nothing() -> None:
    """There is nothing to override, so there is nothing to write."""
    before = parse(DOCUMENT)
    after = set_track_language(before, OTHER_UID, "deu")
    assert after.triples == before.triples


# -------------------------------------------------------------------- provenance
def test_a_provenance_tag_says_what_wrote_what_and_when() -> None:
    tag = provenance(
        "chapter names", "an example source", when=dt.date(2026, 1, 31), tool="mkvkit"
    )
    names = [simple.name for simple in tag.simples]
    assert names == [
        "CHAPTER_NAMES_SOURCE",
        "CHAPTER_NAMES_SOURCE_DATE",
        "CHAPTER_NAMES_SOURCE_TOOL",
    ]
    assert tag.simples[1].value == "2026-01-31"
    assert tag.targets.is_global


def test_merging_keeps_everything_that_was_there() -> None:
    before = parse(DOCUMENT)
    after = merge(before, [provenance("chapter names", "an example source")])
    assert set(before.triples) <= set(after.triples)
    assert ("", "CHAPTER_NAMES_SOURCE", "an example source") in after.triples


def test_merging_the_same_provenance_twice_leaves_one() -> None:
    """A pass that is run again must leave the file as the first run left it."""
    tag = provenance("chapter names", "an example source")
    once = merge(parse(DOCUMENT), [tag])
    twice = merge(once, [tag])
    assert once.triples == twice.triples


def test_merging_a_new_value_replaces_the_old_one() -> None:
    first = merge(parse(DOCUMENT), [provenance("chapter names", "one source")])
    second = merge(first, [provenance("chapter names", "another source")])
    values = [value for _, name, value in second.triples if name.endswith("_SOURCE")]
    assert values == ["another source"]


def test_a_targeted_provenance_tag_does_not_disturb_the_global_one() -> None:
    targeted = provenance(
        "language", "a detector", targets=Targets(track_uids=(TRACK_UID,))
    )
    after = merge(parse(DOCUMENT), [targeted])
    assert after.language_of(TRACK_UID) == "fra"
    assert (str(TRACK_UID), "LANGUAGE_SOURCE", "a detector") in after.triples


RICH = f"""<?xml version="1.0"?>
<Tags>
  <Tag>
    <Targets><TrackUID>{TRACK_UID}</TrackUID></Targets>
    <Simple>
      <Name>ARTIST</Name>
      <String>A. Example</String>
      <TagLanguage>eng</TagLanguage>
      <TagLanguageIETF>en-GB</TagLanguageIETF>
      <DefaultLanguage>0</DefaultLanguage>
      <Simple>
        <Name>SORT_WITH</Name>
        <String>Example, A.</String>
      </Simple>
    </Simple>
    <Simple>
      <Name>COVER_HASH</Name>
      <Binary format="hex">0a0b0c</Binary>
    </Simple>
  </Tag>
</Tags>
"""


def test_a_round_trip_keeps_nesting_binary_values_and_the_language_fields() -> None:
    """The document is written back whole: whatever a read drops, a write deletes."""
    read = parse(RICH)
    again = parse(build(read))
    assert again == read
    artist = read.tags[0].simples[0]
    assert artist.language_ietf == "en-GB"
    assert artist.default == "0"
    assert [child.name for child in artist.children] == ["SORT_WITH"]
    blob = read.tags[0].simples[1]
    assert (blob.binary, blob.binary_format) == ("0a0b0c", "hex")
    written = build(read)
    assert '<Binary format="hex">0a0b0c</Binary>' in written
    assert "<TagLanguageIETF>en-GB</TagLanguageIETF>" in written


def test_the_comparison_sees_a_nested_tag_that_came_back_flattened() -> None:
    read = parse(RICH)
    artist = read.tags[0].simples[0]
    flattened = TagSet(
        (Tag(read.tags[0].targets,
             (SimpleTag(artist.name, artist.value), *artist.children,
              read.tags[0].simples[1])),)
    )
    assert flattened.triples != read.triples
    assert (str(TRACK_UID), "ARTIST/SORT_WITH", "Example, A.") in read.triples


def test_the_comparison_sees_a_binary_value_that_came_back_empty() -> None:
    read = parse(RICH)
    emptied = parse(RICH.replace("0a0b0c", ""))
    assert emptied.triples != read.triples


def test_a_document_with_elements_this_does_not_carry_is_never_written() -> None:
    odd = RICH.replace(
        "<Name>COVER_HASH</Name>", "<Name>COVER_HASH</Name><Something>x</Something>"
    )
    read = parse(odd)
    assert read.unkept == ("Simple/Something",)
    with pytest.raises(TagError, match="Simple/Something"):
        build(read)
    # merging or rewriting a language does not launder it
    with pytest.raises(TagError):
        build(merge(read, [provenance("chapter names", "an example source")]))
    with pytest.raises(TagError):
        build(set_track_language(read, TRACK_UID, "deu"))


def test_a_tag_set_can_be_built_by_hand() -> None:
    tags = TagSet(
        (
            Tag(
                targets=Targets(track_uids=(TRACK_UID,)),
                simples=(SimpleTag("LANGUAGE", "deu"),),
            ),
        )
    )
    assert parse(build(tags)).language_of(TRACK_UID) == "deu"


# -------------------------------------------------------------- against the file
@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_fixture_carries_a_tag_that_contradicts_its_header(
    media_fixtures: dict[str, Path],
) -> None:
    from mkvkit.probe import probe

    from tests.fixtures import TAG_OVERRIDE_LANGUAGE

    path = media_fixtures["tag_override.mkv"]
    tags = read_tags(path)
    found = probe(path)
    track = found.audio[0]
    assert track.uid is not None
    assert tags.language_of(track.uid) == TAG_OVERRIDE_LANGUAGE
    assert track.language == "eng"
    assert [d.track_id for d in found.language_disagreements] == [track.id]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_the_pre_existing_tags_of_the_fixture_survived_the_addition(
    media_fixtures: dict[str, Path],
) -> None:
    """The fixture is built by merging, because writing tags replaces them all."""
    tags = read_tags(media_fixtures["tag_override.mkv"])
    names = {name for _, name, _ in tags.triples}
    assert "LANGUAGE" in names
    assert names - {"LANGUAGE"}


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_mkvtoolnix
def test_a_file_with_no_tags_reads_as_an_empty_set(
    media_fixtures: dict[str, Path],
) -> None:
    assert isinstance(read_tags(media_fixtures["chapter_grid.mkv"]), TagSet)
