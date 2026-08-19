import pytest

from salmon.config.validations import ImageHostOverride, ImageUploader


def test_resolve_global_fallback():
    i = ImageUploader()
    assert i.resolve("RED", "cover_uploader") == "catbox"
    assert i.resolve("OPS", "specs_uploader") == "catbox"


def test_resolve_per_tracker_override():
    i = ImageUploader(cover_uploader="catbox", red=ImageHostOverride(cover_uploader="red"))
    assert i.resolve("RED", "cover_uploader") == "red"       # RED override
    assert i.resolve("OPS", "cover_uploader") == "catbox"     # OPS falls back to global
    assert i.resolve("RED", "image_uploader") == "catbox"     # unset field falls back
    assert i.resolve(None, "cover_uploader") == "catbox"      # no site -> global


def test_specs_red_blocked_globally_and_per_tracker():
    with pytest.raises(ValueError, match="spectral"):
        ImageUploader(specs_uploader="red")
    with pytest.raises(ValueError, match="spectral"):
        ImageUploader(red=ImageHostOverride(specs_uploader="red"))


def test_cover_red_blocked_for_non_red_tracker():
    with pytest.raises(ValueError, match="OPS"):
        ImageUploader(ops=ImageHostOverride(cover_uploader="red"))
    with pytest.raises(ValueError, match="DIC"):
        ImageUploader(dic=ImageHostOverride(image_uploader="red"))


def test_cover_red_allowed_for_red():
    ImageUploader(red=ImageHostOverride(cover_uploader="red"))  # must not raise
