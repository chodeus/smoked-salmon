"""upload_cover: a host that returns without raising must still fail if it hands back no usable URL."""

from types import SimpleNamespace

import anyio
import pytest

from salmon.images import HOSTS, upload_cover


class _FakeUploader:
    def __init__(self, url: str | None):
        self._url = url

    async def upload_file(self, _filename: str):
        return self._url, None


def _stub_host(monkeypatch, url: str | None) -> None:
    monkeypatch.setitem(HOSTS, "catbox", SimpleNamespace(ImageUploader=lambda: _FakeUploader(url)))


def test_no_path_reports_failure_without_touching_any_host() -> None:
    result = anyio.run(upload_cover, None, "OPS")
    assert result is None


def test_a_real_url_is_returned(monkeypatch, tmp_path) -> None:
    _stub_host(monkeypatch, "https://files.catbox.moe/abc.jpg")
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"data")
    result = anyio.run(upload_cover, str(image), "OPS")
    assert result == "https://files.catbox.moe/abc.jpg"


# None is reachable: imgbox returns submission["image_url"], which pyimgbox documents as "URL to image or None".
@pytest.mark.parametrize("url", ["", None, "https://", "https:///abc.jpg"])
def test_an_unusable_url_is_treated_as_a_failure_not_a_success(monkeypatch, tmp_path, url) -> None:
    # A host can return 200 with nothing usable; upload_cover must not cache/report that as done.
    _stub_host(monkeypatch, url)
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"data")
    result = anyio.run(upload_cover, str(image), "OPS")
    assert result is None
