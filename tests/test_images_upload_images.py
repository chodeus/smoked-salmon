"""upload_images: the description/spectral path must refuse a host response the cover path would refuse."""

from types import SimpleNamespace

import anyio
import pytest

from salmon.errors import ImageUploadFailed
from salmon.images import upload_images


class _FakeUploader:
    def __init__(self, url: str | None):
        self._url = url

    async def upload_file(self, _filename: str):
        return self._url, None


def _host(url: str | None) -> SimpleNamespace:
    return SimpleNamespace(ImageUploader=lambda: _FakeUploader(url), __name__="salmon.images.fake")


def test_a_real_url_is_collected(tmp_path) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(b"data")
    urls = anyio.run(upload_images, [str(image)], _host("https://files.catbox.moe/a.png"))
    assert urls == ["https://files.catbox.moe/a.png"]


@pytest.mark.parametrize("url", ["", None, "https://"])
def test_an_unusable_url_raises_instead_of_landing_in_a_description(tmp_path, url) -> None:
    image = tmp_path / "a.png"
    image.write_bytes(b"data")
    with pytest.raises(ImageUploadFailed):
        anyio.run(upload_images, [str(image)], _host(url))
