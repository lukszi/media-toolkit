"""Context priors: what they claim, and everything they refuse to claim.

Half of these tests assert that a prior does *nothing*. That is the right
proportion. A prior is evidence that correlates with the mistakes it would
confirm, so the interesting behaviour is where it declines -- on extras, on
commentary, on short clips, and whenever two context signals disagree.

Every name here is invented.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import pytest
from mkvkit.langid.priors import (
    Prior,
    SiblingVote,
    blocking_reasons,
    combine,
    release_token_prior,
    sibling_prior,
    track_kind,
    trimmed_mean,
)

SERIES = "/srv/media/series"


# ----------------------------------------------------------- the release prior
def test_a_single_language_claim_on_a_single_track_file() -> None:
    prior = release_token_prior("Northwind.S01E03.German.mkv", audio_track_count=1)
    assert (prior.language, prior.strength) == ("deu", "medium")


def test_a_dual_language_claim_resolves_only_when_the_others_are_known() -> None:
    """Two claimed languages say nothing about *this* track on their own.

    They become useful exactly when every other track is accounted for and one
    claim is left over -- which is arithmetic, not a guess.
    """
    ambiguous = release_token_prior("Northwind.S01E03.German.DL.mkv", audio_track_count=2)
    assert ambiguous.language is None

    resolved = release_token_prior(
        "Northwind.S01E03.German.DL.mkv", audio_track_count=2, known=["deu"]
    )
    assert resolved.language == "eng"


def test_a_claim_that_names_no_language_is_not_a_claim() -> None:
    assert release_token_prior("Northwind.S01E03.mkv").language is None
    assert release_token_prior("Northwind.S01E03.Multi.mkv").language is None


def test_the_parent_directory_counts_as_part_of_the_name() -> None:
    """Release naming puts the tokens on the folder as often as on the file."""
    prior = release_token_prior(
        f"{SERIES}/Northwind.S01.French/episode-03.mkv", audio_track_count=1
    )
    assert prior.language == "fra"


def test_the_tokens_are_matched_as_tokens_not_as_substrings() -> None:
    """Otherwise every title containing a nationality becomes a language claim."""
    assert release_token_prior("The Germans Arrive.mkv", audio_track_count=1).language is None


# ----------------------------------------------------------- the sibling prior
def test_three_unanimous_neighbours_are_a_medium_prior() -> None:
    votes = [SiblingVote("deu") for _ in range(3)]
    prior = sibling_prior(votes)
    assert (prior.language, prior.strength) == ("deu", "medium")


def test_ten_agreeing_neighbours_are_a_strong_prior_even_with_a_dissenter() -> None:
    votes = [SiblingVote("deu") for _ in range(10)] + [SiblingVote("eng")]
    prior = sibling_prior(votes)
    assert (prior.language, prior.strength) == ("deu", "strong")


def test_five_unanimous_neighbours_are_also_strong() -> None:
    """Either pair qualifies: ten at ninety per cent, or five that agree."""
    assert sibling_prior([SiblingVote("deu")] * 5).strength == "strong"


def test_nine_of_ten_is_not_yet_strong() -> None:
    votes = [SiblingVote("deu") for _ in range(9)] + [SiblingVote("eng")]
    assert sibling_prior(votes).strength is None


def test_two_neighbours_are_not_a_prior_at_all() -> None:
    assert sibling_prior([SiblingVote("deu"), SiblingVote("deu")]).strength is None


def test_a_divided_season_records_the_majority_without_any_strength() -> None:
    """Recording what the context said, without letting it lower a bar."""
    votes = [SiblingVote("deu")] * 3 + [SiblingVote("eng")] * 2
    prior = sibling_prior(votes)
    assert prior.language == "deu"
    assert prior.strength is None
    assert not prior


# ------------------------------------------------------------------- blocking
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"{SERIES}/Northwind/Extras/Northwind - behind the scenes.mkv", "extra"),
        (f"{SERIES}/Northwind/Featurettes/a-featurette.mkv", "extra"),
        (f"{SERIES}/Northwind/Deleted Scenes/one.mkv", "extra"),
        (f"{SERIES}/Northwind/Trailers/teaser.mkv", "extra"),
    ],
)
def test_material_that_is_not_the_programme_blocks_the_context(
    path: str, expected: str
) -> None:
    assert expected in blocking_reasons(path)


def test_a_commentary_track_blocks_the_context() -> None:
    """It is in the language of the people talking over the film, not of it."""
    reasons = blocking_reasons(f"{SERIES}/Northwind/one.mkv", title="Director's Commentary")
    assert "commentary" in reasons


def test_a_clip_too_short_to_be_an_episode_blocks_the_context() -> None:
    assert "clip" in blocking_reasons(f"{SERIES}/Northwind/one.mkv", runtime_s=45.0)


def test_a_disc_folder_blocks_the_context() -> None:
    """The order of audio tracks inside a resolved stream is not the reported order."""
    assert "disc-folder" in blocking_reasons(f"{SERIES}/Northwind/one", is_disc_folder=True)


def test_ordinary_material_blocks_nothing() -> None:
    assert blocking_reasons(
        f"{SERIES}/Northwind/Season 01/Northwind - S01E03.mkv",
        title="Surround 5.1", runtime_s=2700.0,
    ) == ()


def test_a_windows_style_path_is_read_the_same_way() -> None:
    path = "C:" + "\\" + "Media" + "\\" + "Northwind" + "\\" + "Extras" + "\\" + "one.mkv"
    assert "extra" in blocking_reasons(path)


def test_track_kinds_are_recognised_case_insensitively() -> None:
    assert track_kind("AUDIO DESCRIPTION").kind == "descriptive"
    assert track_kind("Isolated Score").kind == "score"
    assert track_kind(None).kind is None


# ------------------------------------------------------------------ combining
def test_two_priors_that_agree_take_the_stronger_strength() -> None:
    combined = combine(
        Prior(language="deu", strength="strong", sources=("sibling:season:9/9",)),
        Prior(language="deu", strength="medium", sources=("release:german",)),
    )
    assert (combined.language, combined.strength) == ("deu", "strong")
    assert len(combined.sources) == 2


def test_two_priors_that_disagree_cancel_rather_than_vote() -> None:
    """A conflict says the context is not what it looks like."""
    combined = combine(
        Prior(language="deu", strength="strong", sources=("sibling:season:9/9",)),
        Prior(language="eng", strength="medium", sources=("release:english",)),
    )
    assert combined.language is None
    assert combined.conflict == "deu+eng"
    assert not combined


def test_a_blocked_prior_is_empty_whatever_it_would_have_said() -> None:
    combined = combine(
        Prior(language="deu", strength="strong", sources=("sibling:season:9/9",)),
        blocked=["commentary"],
    )
    assert combined.language is None
    assert combined.blocked == ("commentary",)
    assert not combined


# ---------------------------------------------------------------------- trims
def test_the_trimmed_mean_drops_the_least_favourable_values() -> None:
    values = [0.99, 0.98, 0.99, 0.99, 0.02]
    assert trimmed_mean(values, 0.2) == pytest.approx(0.9875)
    assert trimmed_mean(values, 0.0) == pytest.approx(sum(values) / len(values))


def test_the_trimmed_mean_of_nothing_is_zero_not_an_error() -> None:
    assert trimmed_mean([], 0.2) == 0.0
