"""Genre standardization: splitting combined genres without breaking whitelisted ones."""

from salmon.common import split_genre
from salmon.tagger.pre_data import split_genres
from salmon.tagger.sources.base import standardize_genres


def test_slash_combined_genres_are_split_into_whitelisted_parts():
    # "dancepop" is not a whitelist key; each half is.
    assert sorted(standardize_genres({"Dance / Pop"})) == ["Dance", "Pop"]
    assert sorted(standardize_genres({"Funk / Soul / Disco"})) == ["Disco", "Funk", "Soul"]


def test_ampersand_genres_are_left_whole():
    # Single entries in GENRE_LIST; splitting would invent "Drum" and "Bass".
    assert standardize_genres({"Drum & Bass"}) == ["Drum & Bass"]
    assert standardize_genres({"Rhythm & Blues"}) == ["Rhythm & Blues"]
    # "R&B" keys to "randb", which the whitelist canonicalizes rather than splits.
    assert standardize_genres({"R&B"}) == ["Rhythm & Blues"]


def test_splitting_no_longer_evicts_the_standalone_genre():
    # The combination filter must not discard a standalone genre the release also carries.
    assert sorted(standardize_genres({"Dance", "Dance / Pop"})) == ["Dance", "Pop"]


def test_unknown_genres_survive_and_whitespace_is_dropped():
    assert standardize_genres({"Bhangra"}) == ["Bhangra"]
    assert sorted(standardize_genres({"House /  / Folk"})) == ["Folk", "House"]


def test_split_genre_never_splits_on_an_ampersand():
    # "&" is a separator for artists but not for genres; the whitelist stores these whole.
    assert split_genre("Drum & Bass") == ["Drum & Bass"]
    assert split_genre("Rock & Roll") == ["Rock & Roll"]
    assert split_genre("R&B") == ["R&B"]


def test_split_genre_splits_the_genre_separators():
    assert split_genre("Dance / Pop") == ["Dance", "Pop"]
    assert split_genre("Rock; Pop, Jazz") == ["Rock", "Pop", "Jazz"]
    assert split_genre("House") == ["House"]
    assert split_genre("  ") == []


def test_file_tag_genres_keep_their_ampersands():
    # split_genres must use the genre splitter, not re_split, which treats " & " as a separator.
    assert split_genres(["Drum & Bass"]) == ["Drum & Bass"]
    assert sorted(split_genres(["Rock; Pop"])) == ["Pop", "Rock"]


def test_qobuz_hierarchy_arrows_split_and_map_to_english():
    # Qobuz returns a localized hierarchy path; "electronique" is a whitelist key, "electroniquedance" is not.
    assert split_genre("\u00c9lectronique\u2192Dance") == ["\u00c9lectronique", "Dance"]
    assert sorted(standardize_genres({"Electronic", "\u00c9lectronique\u2192Dance"})) == ["Dance", "Electronic"]
    assert sorted(standardize_genres({"Pop/Rock\u2192Rock"})) == ["Pop", "Rock"]
