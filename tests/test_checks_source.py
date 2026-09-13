import pytest
from mutagen.id3 import COMM
from mutagen.mp4 import MP4FreeForm

from salmon.checks import source as src


class _FakeInfo:
    def __init__(self, bits, rate):
        self.bits_per_sample = bits
        self.sample_rate = rate


class _FakeAudio:
    def __init__(self, tags, bits, rate):
        self.tags = tags
        self.info = _FakeInfo(bits, rate)


@pytest.fixture
def tagged(monkeypatch):
    """Give every audio file in the album the same tags and audio properties."""

    def _apply(tags, bits=16, rate=44100):
        monkeypatch.setattr(src, "MutagenFile", lambda _path: _FakeAudio(tags, bits, rate))

    return _apply


def test_rip_log_proves_cd(album_dir, tagged):
    tagged({"artist": "X", "album": "Y", "tracknumber": "1"})
    (album_dir / "rip.log").write_text("Exact Audio Copy V1.6 from 23. October 2020\n\nUsed drive : ASUS")
    result = src.detect_source(str(album_dir))
    assert result["source"] == "CD"
    assert result["confidence"] == "confirmed"


def test_media_tag_is_taken_at_its_word(album_dir, tagged):
    tagged({"media": "Digital Media", "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] == "WEB"
    assert result["confidence"] == "confirmed"


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"asin": ["B000123"]}, None),
        ({"COMM::eng": COMM(encoding=3, lang="eng", desc="", text=["Amazon.com Song ID: 200000707885981"])}, "WEB"),
        ({"----:com.apple.iTunes:BARCODE": [MP4FreeForm(b"0602448406705")]}, None),
        ({"----:com.apple.iTunes:MEDIA": [MP4FreeForm(b"CD")]}, "CD"),
        ({"apID": ["someone@example.com"]}, "WEB"),
        ({"purd": ["2020-01-01 10:00:00"]}, "WEB"),
    ],
    ids=[
        "picard-asin",
        "amazon-song-id-comment",
        "m4a-custom-atom",
        "m4a-media-atom",
        "itunes-apple-id",
        "itunes-purchase-date",
    ],
)
def test_only_tags_a_store_writes_prove_web(album_dir, tagged, tags, expected):
    """Picard copies ASIN from MusicBrainz, and every tagger files custom M4A atoms under com.apple.iTunes."""
    tagged({**tags, "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] == expected


def test_hi_res_rules_out_cd_but_is_only_likely_web(album_dir, tagged):
    tagged({"artist": "X", "album": "Y", "tracknumber": "1"}, bits=24, rate=96000)
    result = src.detect_source(str(album_dir))
    assert result["source"] == "WEB"
    assert result["confidence"] == "likely"
    assert any("vinyl" in r.lower() for r in result["reasons"])


def test_vinyl_side_numbering_beats_hi_res_web_guess(album_dir, tagged):
    tagged({"artist": "X", "album": "Y", "tracknumber": "A1"}, bits=24, rate=96000)
    result = src.detect_source(str(album_dir))
    assert result["source"] == "Vinyl"


def test_plain_cd_quality_with_no_log_is_undecidable(album_dir, tagged):
    """The slskd case: 16/44 FLAC could equally be a logless CD rip or a WEB download."""
    tagged({"artist": "X", "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] is None
    assert result["confidence"] == "unknown"


def test_cue_sheet_is_mentioned_but_does_not_decide(album_dir, tagged):
    tagged({"artist": "X", "album": "Y", "tracknumber": "1"})
    (album_dir / "album.cue").write_text('FILE "x.flac" WAVE')
    result = src.detect_source(str(album_dir))
    assert result["source"] is None
    assert any("cue" in r.lower() for r in result["reasons"])


def test_unreadable_audio_does_not_crash(album_dir, monkeypatch):
    monkeypatch.setattr(src, "MutagenFile", lambda _path: None)
    result = src.detect_source(str(album_dir))
    assert result["confidence"] == "unknown"


def test_corrupt_audio_file_does_not_sink_the_scan(album_dir):
    """MutagenFile raises on a truncated file rather than returning None."""
    result = src.detect_source(str(album_dir))
    assert result["confidence"] == "unknown"


def test_cue_in_a_subdirectory_is_found(album_dir, tagged):
    """Cue sheets often sit beside the audio in a disc subfolder, not at the root."""
    tagged({"artist": "X", "album": "Y", "tracknumber": "1"})
    disc = album_dir / "CD1"
    disc.mkdir()
    (disc / "album.cue").write_text('FILE "x.flac" WAVE')
    result = src.detect_source(str(album_dir))
    assert any("cue" in r.lower() for r in result["reasons"])


def test_hi_res_with_no_readable_rate_does_not_claim_0khz(album_dir, monkeypatch):
    class _Info:
        bits_per_sample = 24
        sample_rate = None

    class _Audio:
        tags = {"artist": "X", "album": "Y"}
        info = _Info()

    monkeypatch.setattr(src, "MutagenFile", lambda _p: _Audio())
    result = src.detect_source(str(album_dir))
    assert result["source"] == "WEB"
    assert "0kHz" not in " ".join(result["reasons"])


@pytest.mark.parametrize(
    "tags",
    [
        {"qobuz url": ["https://www.qobuz.com/album/journaling-illy/ul39e7xjbuqrb"]},
        {"source": ["https://www.deezer.com/album/322064097"]},
        {"url": ["https://listen.tidal.com/album/2468665"]},
        {"comment": ["https://music.apple.com/au/album/journaling/1623086473"]},
    ],
    ids=["qobuz-url-key", "deezer-source-key", "tidal-url-key", "apple-music-comment"],
)
def test_store_url_in_any_tag_proves_web(album_dir, tagged, tags):
    tagged({**tags, "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] == "WEB"
    assert result["confidence"] == "confirmed"


def test_database_links_do_not_prove_web(album_dir, tagged):
    """A MusicBrainz purchase link describes the release, not where these files came from."""
    link = "https://artist.bandcamp.com/album/y"
    tagged({"musicbrainz_relationship_url__purchase for download": [link], "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] is None
