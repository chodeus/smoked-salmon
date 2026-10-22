"""A new group's cover goes to its tracker's cover_uploader; a cover already on that host this run is reused."""

import anyio

import salmon.uploader
from salmon import cfg
from salmon.config.validations import TrackerImageSettings

RED_COVER = "https://redacted.sh/i/x.jpg"


def _uploads(monkeypatch) -> list[str | None]:
    uploaded: list[str | None] = []

    async def download(path: str, url: str | None) -> tuple[str, bool]:
        return f"{path}/cover.jpg", False

    async def upload(cover_path: str | None, host: str | None = None, red_api: object = None) -> str:
        uploaded.append(host)
        return f"https://{host}/new.jpg"

    monkeypatch.setattr(salmon.uploader, "download_cover_if_nonexistent", download)
    monkeypatch.setattr(salmon.uploader, "upload_cover", upload)
    return uploaded


def test_ops_uploads_to_its_own_host_though_a_red_hosted_cover_is_at_hand(monkeypatch) -> None:
    monkeypatch.setattr(cfg.image, "ops", None)
    uploaded = _uploads(monkeypatch)
    stored: dict[str, str | None] = {"red": RED_COVER}

    url, _ = anyio.run(salmon.uploader.get_cover_url, "OPS", stored, "/album", None, False)

    assert url == f"https://{cfg.image.cover_uploader}/new.jpg"
    assert uploaded == [cfg.image.cover_uploader]


def test_ops_set_to_reds_host_reuses_the_red_hosted_cover(monkeypatch) -> None:
    monkeypatch.setattr(cfg.image, "ops", TrackerImageSettings(cover_uploader="red"))
    uploaded = _uploads(monkeypatch)
    stored: dict[str, str | None] = {"red": RED_COVER}

    url, _ = anyio.run(salmon.uploader.get_cover_url, "OPS", stored, "/album", None, False)

    assert url == RED_COVER
    assert uploaded == []
