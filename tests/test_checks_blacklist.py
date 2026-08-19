from salmon.checks.blacklist import red_blacklist_reason


def test_blocks_exact_album():
    reason = red_blacklist_reason([("Wu-Tang Clan", "main")], "Once Upon a Time in Shaolin")
    assert reason and "Shaolin" in reason


def test_blocks_whole_discography_artist():
    reason = red_blacklist_reason([("Nicole 12", "main")], "Any Album At All")
    assert reason and "discography" in reason


def test_blocks_by_label():
    reason = red_blacklist_reason([("Whoever", "main")], "Whatever", label="Sandero Classic Sound")
    assert reason is not None


def test_allows_normal_release():
    assert red_blacklist_reason([("Radiohead", "main")], "In Rainbows") is None


def test_album_specific_does_not_block_other_albums_by_same_artist():
    assert red_blacklist_reason([("Dr. Dre", "main")], "2001") is None
    assert red_blacklist_reason([("Dr. Dre", "main")], "Detox") is not None


def test_punctuation_and_case_insensitive():
    assert red_blacklist_reason([("dr dre", "main")], "detox") is not None


def test_plain_string_artists_accepted():
    assert red_blacklist_reason(["Wu-Tang Clan"], "Once Upon a Time in Shaolin") is not None


def test_label_needs_all_words_present():
    # a release with an unrelated label is not blocked by a label entry
    assert red_blacklist_reason([("X", "main")], "Y", label="Real Records") is None
