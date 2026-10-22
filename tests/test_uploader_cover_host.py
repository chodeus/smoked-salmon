"""A new group's cover goes to its tracker's cover_uploader; a cover already on that host this run is reused."""

import anyio

from salmon import cfg
from salmon.config.validations import TrackerImageSettings
from salmon.uploader import cover

RED_COVER = "https://redacted.sh/i/x.jpg"


def _uploads(monkeypatch) -> list[str | None]:
    uploaded: list[str | None] = []

    async def download(path: str, url: str | None) -> tuple[str, bool]:
        return f"{path}/cover.jpg", False

    async def upload(cover_path: str | None, site_code: str | None = None) -> str:
        uploaded.append(site_code)
        return "https://files.catbox.moe/new.jpg"

    monkeypatch.setattr(cover, "download_cover_if_nonexistent", download)
    monkeypatch.setattr(cover, "upload_cover", upload)
    return uploaded


def test_ops_uploads_to_its_own_host_though_a_red_hosted_cover_is_at_hand(monkeypatch) -> None:
    monkeypatch.setattr(cfg.image, "ops", None)
    uploaded = _uploads(monkeypatch)
    stored = {"red": RED_COVER}

    url = anyio.run(cover._new_group_cover_url, "OPS", stored, "/album", None, False)

    assert url == "https://files.catbox.moe/new.jpg"
    assert uploaded == ["OPS"]


def test_ops_set_to_reds_host_reuses_the_red_hosted_cover(monkeypatch) -> None:
    monkeypatch.setattr(cfg.image, "ops", TrackerImageSettings(cover_uploader="red"))
    uploaded = _uploads(monkeypatch)
    stored = {"red": RED_COVER}

    url = anyio.run(cover._new_group_cover_url, "OPS", stored, "/album", None, False)

    assert url == RED_COVER
    assert uploaded == []
