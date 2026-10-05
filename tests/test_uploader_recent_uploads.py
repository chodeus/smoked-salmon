"""The site log check for recent uploads: a shared artist alone is no dupe (ported from upstream #509, #518)."""

from typing import TYPE_CHECKING, cast

import anyio
import pytest

from salmon.uploader import dupe_checker
from salmon.uploader.dupe_checker import (
    _prompt_for_recent_upload_results,
    _recent_upload_matches,
    _title_words,
    dupe_check_recent_torrents,
    generate_dupe_check_searchstrs,
)

if TYPE_CHECKING:
    from salmon.trackers.base import BaseGazelleApi


class FakeGazelleSite:
    site_string = "RED"
    base_url = "http://127.0.0.1"


def test_recent_upload_prompt_keeps_master_wording_when_there_are_no_recent_uploads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the group search found nothing and the site log has no recent uploads either, the
    "similar recent uploads" header is misleading: there is nothing to pick from. Master's plain
    "Would you like to upload to an existing group?" wording should be shown instead (#518 follow-up).
    """
    prompt_text = ""

    async def fake_prompt(text: str, *_args, **_kwargs) -> str:
        nonlocal prompt_text
        prompt_text = text
        return "n"

    monkeypatch.setattr(dupe_checker.click, "prompt", fake_prompt)

    gazelle_site = cast("BaseGazelleApi", cast("object", FakeGazelleSite()))
    anyio.run(_prompt_for_recent_upload_results, gazelle_site, [], "some search", True)

    assert "Would you like to upload to an existing group?" in prompt_text
    assert "similar recent uploads" not in prompt_text
    assert "not exact group matches" not in prompt_text
    out = capsys.readouterr().out
    assert "Found similar recent uploads" not in out


def test_recent_upload_match_requires_more_than_shared_artist_prefix() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["Anna Zak", "main"]], "קלטתי אותך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is False


def test_recent_upload_match_uses_all_generated_search_strings() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["אביב גפן", "main"]], "מה נשאר לי ממך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


def test_recent_upload_match_accepts_true_collab_title_match() -> None:
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    comparisons = generate_dupe_check_searchstrs([["Anna Zak & אביב גפן", "main"]], "מה נשאר לי ממך")

    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


def test_recent_upload_match_requires_shared_title_word_when_titles_given() -> None:
    """A shared three-word artist alone can carry a high ratio and word-overlap fraction even with
    a completely different one-word title. Passing normalized title words for both sides requires
    the titles to share a word too (#518 CodeRabbit follow-up on #509)."""
    searchstrs = generate_dupe_check_searchstrs([["John James Smith", "main"]], "Sunrise")
    comparisons = generate_dupe_check_searchstrs([["John James Smith", "main"]], "Sunset")

    assert (
        _recent_upload_matches(
            searchstrs,
            comparisons,
            tolerance=0.5,
            our_title_words=_title_words("Sunrise"),
            candidate_title_words=_title_words("Sunset"),
        )
        is False
    )
    # Without title words, the shared artist alone is still enough (the pre-existing behaviour).
    assert _recent_upload_matches(searchstrs, comparisons, tolerance=0.5) is True


class _LogOnlySite:
    """A minimal stand-in for BaseGazelleApi that only serves get_uploads_from_log()."""

    def __init__(self, uploads: list[tuple]) -> None:
        self._uploads = uploads

    async def get_uploads_from_log(self) -> list[tuple]:
        return self._uploads


def test_dupe_check_recent_torrents_ignores_shared_artist_prefix_only() -> None:
    """A logged upload that shares only an artist with our release must not be flagged as a dupe,
    a true collab title match must still be flagged, and a match that only shows up through a
    search string other than searchstrs[0] must also be flagged (master only checks searchstrs[0]).
    """
    searchstrs = generate_dupe_check_searchstrs([["Anna Zak", "main"], ["אביב גפן", "main"]], "מה נשאר לי ממך")
    false_positive_upload = (1, "Anna Zak", "קלטתי אותך")
    true_match_upload = (2, "Anna Zak & אביב גפן", "מה נשאר לי ממך")
    # Matches only through searchstrs[1] ("אביב גפן" + album), not searchstrs[0] ("Anna Zak" + album).
    second_artist_only_match_upload = (3, "אביב גפן", "מה נשאר לי ממך")
    gazelle_site = cast(
        "BaseGazelleApi",
        cast("object", _LogOnlySite([false_positive_upload, true_match_upload, second_artist_only_match_upload])),
    )

    hits = anyio.run(dupe_check_recent_torrents, gazelle_site, searchstrs)

    assert false_positive_upload not in hits
    assert true_match_upload in hits
    assert second_artist_only_match_upload in hits


def test_dupe_check_recent_torrents_requires_shared_title_word_not_just_shared_artist() -> None:
    """A three-word shared artist can carry a high SequenceMatcher ratio and word-overlap fraction
    on its own, even with a completely different one-word title ("john james smith sunrise" vs
    "john james smith sunset" share 3 of 4 words). Passing our release's title makes
    dupe_check_recent_torrents require the titles themselves to share a word, while a same-title
    match (including a collab) still hits (#518 CodeRabbit follow-up).
    """
    artist = [["John James Smith", "main"]]
    our_title = "Sunrise"
    searchstrs = generate_dupe_check_searchstrs(artist, our_title)
    different_title_upload = (1, "John James Smith", "Sunset")
    same_title_upload = (2, "John James Smith", "Sunrise")
    collab_title_upload = (3, "John James Smith & Someone Else", "Sunrise")
    gazelle_site = cast(
        "BaseGazelleApi",
        cast("object", _LogOnlySite([different_title_upload, same_title_upload, collab_title_upload])),
    )

    hits = anyio.run(dupe_check_recent_torrents, gazelle_site, searchstrs, our_title)

    assert different_title_upload not in hits
    assert same_title_upload in hits
    assert collab_title_upload in hits
