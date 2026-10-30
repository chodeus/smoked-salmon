"""A store link with a query or fragment (a share link, a tab) names the same release as its plain form."""

import anyio
import musicbrainzngs
import pytest

from salmon.sources.musicbrainz import MusicBrainzBase
from salmon.tagger import metadata as metadata_mod


def test_a_tracked_bandcamp_link_is_the_same_album_as_its_plain_one() -> None:
    plain = metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album")

    assert plain is not None
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album?from=fanpub_fnb") == plain
    assert metadata_mod._album_identity("https://artist.bandcamp.com/album/some-album#buyFullDiscography") == plain


@pytest.mark.parametrize(
    ("url", "source"),
    [
        ("https://open.qobuz.com/album/0887396827479?utm_source=share", "Qobuz"),
        ("https://www.qobuz.com/gb-en/album/some-album/0887396827479#tracks", "Qobuz"),
        ("https://www.beatport.com/release/some-release/1234567?utm_source=share", "Beatport"),
        ("https://musicbrainz.org/release/0f3c8f2a-1b2c-4d5e-8f90-123456789abc?tab=tracks", "MusicBrainz"),
    ],
)
def test_a_store_link_with_a_query_is_its_own_stores(url, source) -> None:
    assert metadata_mod._metasource_of(url) == source


def test_a_musicbrainz_link_with_a_query_fetches_its_release(monkeypatch) -> None:
    asked = []

    def get_release_by_id(rls_id, includes):
        asked.append(rls_id)
        return {"release": {}}

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", get_release_by_id)
    url = "https://musicbrainz.org/release/0f3c8f2a-1b2c-4d5e-8f90-123456789abc/?tab=tracks"

    anyio.run(lambda: MusicBrainzBase().fetch_data(url))

    assert asked == ["0f3c8f2a-1b2c-4d5e-8f90-123456789abc"]
