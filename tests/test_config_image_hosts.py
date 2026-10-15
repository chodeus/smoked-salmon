import pytest

from salmon.config.validations import ImageHostOverride, ImageUploader


def test_resolve_global_fallback():
    i = ImageUploader()
    assert i.resolve("OPS", "cover_uploader") == "catbox"
    assert i.resolve("OPS", "specs_uploader") == "catbox"
    assert i.resolve("RED", "image_uploader") == "catbox"
    assert i.resolve(None, "cover_uploader") == "catbox"


def test_red_covers_need_opting_in():
    # RED's own host is opt-in: without [image.red] a RED cover goes wherever [image] says.
    assert ImageUploader().resolve("RED", "cover_uploader") == "catbox"
    assert ImageUploader(cover_uploader="imgbox").resolve("RED", "cover_uploader") == "imgbox"
    opted_in = ImageUploader(red=ImageHostOverride(cover_uploader="red"))
    assert opted_in.resolve("RED", "cover_uploader") == "red"


def test_resolve_per_tracker_override():
    i = ImageUploader(cover_uploader="catbox", red=ImageHostOverride(cover_uploader="imgbox"))
    assert i.resolve("RED", "cover_uploader") == "imgbox"  # per-tracker override wins
    assert i.resolve("OPS", "cover_uploader") == "catbox"  # OPS falls back to global
    assert i.resolve("RED", "image_uploader") == "catbox"  # unset field falls back
    assert i.resolve(None, "cover_uploader") == "catbox"  # no site -> global


def test_red_allowed_as_red_cover_host():
    ImageUploader(red=ImageHostOverride(cover_uploader="red"))  # must not raise


@pytest.mark.parametrize(
    ("kwargs", "section"),
    [
        ({"cover_uploader": "red"}, "[image]"),
        ({"image_uploader": "red"}, "[image]"),
        ({"specs_uploader": "red"}, "[image]"),
        ({"red": ImageHostOverride(image_uploader="red")}, "[image.red]"),
        ({"red": ImageHostOverride(specs_uploader="red")}, "[image.red]"),
        ({"ops": ImageHostOverride(cover_uploader="red")}, "[image.ops]"),
        ({"dic": ImageHostOverride(image_uploader="red")}, "[image.dic]"),
    ],
)
def test_red_refused_everywhere_else(kwargs, section):
    with pytest.raises(ValueError, match="artwork") as excinfo:
        ImageUploader(**kwargs)
    assert str(excinfo.value).startswith(f"{section} ")


@pytest.mark.parametrize(
    ("kwargs", "section"),
    [
        ({"specs_uploader": "ra"}, "[image]"),
        ({"red": ImageHostOverride(specs_uploader="ra")}, "[image.red]"),
        ({"ops": ImageHostOverride(specs_uploader="ra")}, "[image.ops]"),
    ],
)
def test_ra_refused_for_spectrals(kwargs, section):
    with pytest.raises(ValueError, match="Ra's owner asks not to use it for spectrals") as excinfo:
        ImageUploader(ra_key="key", **kwargs)
    assert str(excinfo.value).startswith(f"{section} specs_uploader")


def test_ra_allowed_for_covers_and_description_images():
    i = ImageUploader(ra_key="key", cover_uploader="ra", ops=ImageHostOverride(image_uploader="ra"))
    assert i.resolve("RED", "cover_uploader") == "ra"
    assert i.resolve("OPS", "image_uploader") == "ra"


def test_ra_needs_its_key():
    with pytest.raises(ValueError, match="ra key not specified"):
        ImageUploader(dic=ImageHostOverride(cover_uploader="ra"))
