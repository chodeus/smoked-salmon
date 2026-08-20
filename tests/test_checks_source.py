import pytest

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


def test_store_tag_proves_web(album_dir, tagged):
    tagged({"asin": "B000123", "album": "Y", "tracknumber": "1"})
    result = src.detect_source(str(album_dir))
    assert result["source"] == "WEB"
    assert result["confidence"] == "confirmed"


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
