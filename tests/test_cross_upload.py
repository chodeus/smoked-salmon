from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anyio
import asyncclick as click
import pytest
from torf import Torrent

import salmon.cross_upload as cross_upload_module
from salmon.common import UploadFiles
from salmon.cross_upload import (
    _compile_data,
    _conversion_options,
    _input_items,
    _missing_conversions,
    _source_response,
    _upload_conversions,
    is_torrent_reference,
)
from salmon.release_notification import upload_footer


class SourceSite:
    base_url = "https://redacted.sh"
    tracker_url = "https://flacsfor.me"
    site_code = site_string = "RED"
    release_types = {"Demo": 17, "Unknown": 21}

    def __init__(self) -> None:
        self.params = None

    async def api_call(self, action, params):
        self.params = (action, params)
        return {"torrent": {"id": 42}}


def test_single_and_batch_inputs(tmp_path: Path) -> None:
    source: Any = SourceSite()
    by_id = _input_items("42", source)
    by_url = _input_items("https://redacted.sh/torrents.php?id=1&torrentid=42", source)
    assert by_id == [42]
    assert by_url == [42]

    release = tmp_path / "release"
    release.mkdir()
    (release / "track.flac").write_bytes(b"audio")
    torrent = Torrent(release, trackers=["https://flacsfor.me/passkey/announce"], private=True, source="RED")
    torrent.generate()
    torrent_file = tmp_path / "release.torrent"
    torrent.write(torrent_file)

    from_directory = _input_items(str(tmp_path), source)
    response = anyio.run(_source_response, torrent_file, source)
    assert from_directory == [torrent_file]
    assert response == {"torrent": {"id": 42}}
    assert source.params == ("torrent", {"hash": torrent.infohash.upper()})


def test_a_numeric_id_is_an_id_even_when_a_folder_of_that_name_exists(tmp_path: Path, monkeypatch) -> None:
    """The web UI skips path confinement for references, so a reference must never become a path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "42").mkdir()
    items = _input_items("42", cast("Any", SourceSite()))
    assert items == [42]


@pytest.mark.parametrize(
    "value",
    [
        "https://redacted.sh/torrents.php?id=1&torrentid=42",
        "https://redacted.sh/../../etc/passwd?torrentid=42",
    ],
)
def test_a_url_reference_resolves_to_its_id_and_not_a_path(value, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    items = _input_items(value, cast("Any", SourceSite()))
    assert items == [42]


@pytest.mark.parametrize("value", ["https://", "https://other.example/torrents.php?torrentid=42"])
def test_a_url_the_endpoint_lets_through_is_refused_rather_than_walked(value, tmp_path: Path, monkeypatch) -> None:
    # It skipped validate_confined_path, so the only safe outcomes are an id or a refusal.
    monkeypatch.chdir(tmp_path)
    skips_confinement = is_torrent_reference(value)
    assert skips_confinement is True
    with pytest.raises(click.UsageError):
        _input_items(value, cast("Any", SourceSite()))


@pytest.mark.parametrize("value", ["/srv/music/album", "album", "~/music", "C:\\music\\album", ""])
def test_a_local_path_is_not_a_reference_so_it_stays_confined(value) -> None:
    confined = is_torrent_reference(value)
    assert confined is False


def test_cross_upload_data_maps_source_to_target() -> None:
    response = {
        "group": {
            "name": "Album &amp; More",
            "year": 2020,
            "releaseType": 17,
            "recordLabel": "Label",
            "catalogueNumber": "CAT-1",
            "tags": ["rock", "demo"],
            "wikiImage": "https://img.example/cover.jpg?a=1&amp;b=2",
            "wikiBBcode": "Group notes",
            "musicInfo": {
                "artists": [{"name": "Main &amp; Artist"}],
                "with": [{"name": "Guest"}],
            },
        },
        "torrent": {
            "id": 42,
            "username": "uploader",
            "userId": 7,
            "description": "Release notes",
            "filePath": "Artist - Album",
            "remasterYear": 2021,
            "remasterTitle": "Deluxe",
            "remasterRecordLabel": "",
            "remasterCatalogueNumber": "",
            "format": "FLAC",
            "encoding": "Lossless",
            "media": "Blu-Ray",
            "scene": True,
        },
    }
    target: Any = SimpleNamespace(
        site_code="OPS",
        release_types={"Demo": 10, "Unknown": 21},
    )

    data = _compile_data(response, cast("Any", SourceSite()), target)

    assert data["title"] == "Album & More"
    assert data["image"] == "https://img.example/cover.jpg?a=1&b=2"
    assert data["artists[]"] == ["Main & Artist", "Guest"]
    assert data["importance[]"] == [1, 2]
    assert data["releasetype"] == 10
    assert data["media"] == "BD"
    assert data["tags"] == "rock,demo"
    assert data["scene"] is True
    assert "torrentid=42" in data["release_desc"]
    assert "[b]RED → OPS[/b]" in data["release_desc"]
    assert "[url=https://redacted.sh/user.php?id=7]uploader[/url]" in data["release_desc"]
    assert "Cross-uploaded with" in data["release_desc"]
    assert "\n\nRelease notes" in data["release_desc"]
    # The source came from another tracker with no footer of ours, so one is added.
    assert data["release_desc"].endswith(upload_footer())
    assert data["release_desc"].count("Uploaded with") == 1


def test_cross_upload_does_not_duplicate_a_footer_the_source_already_has() -> None:
    """A source uploaded with this tool already ends with our footer; it must not gain a second."""
    response: Any = {
        "group": {
            "name": "Album",
            "year": 2020,
            "releaseType": 17,
            "recordLabel": "L",
            "catalogueNumber": "C",
            "tags": ["rock"],
            "wikiImage": "",
            "wikiBBcode": "",
            "musicInfo": {"artists": [{"name": "A"}], "with": []},
        },
        "torrent": {
            "id": 42,
            "username": "u",
            "userId": 7,
            "description": "Release notes\n" + upload_footer(),
            "filePath": "A - Album",
            "remasterYear": 2021,
            "remasterTitle": "",
            "remasterRecordLabel": "",
            "remasterCatalogueNumber": "",
            "format": "FLAC",
            "encoding": "Lossless",
            "media": "WEB",
            "scene": False,
        },
    }
    target: Any = SimpleNamespace(site_code="OPS", release_types={"Demo": 10, "Unknown": 21})

    data = _compile_data(response, cast("Any", SourceSite()), target)

    assert data["release_desc"].count("Uploaded with") == 1


def test_conversion_uploads_share_original_group(tmp_path: Path, monkeypatch) -> None:
    async def fake_convert(_path):
        return 44100, str(tmp_path / "16bit")

    async def fake_transcode(_path, bitrate):
        return str(tmp_path / bitrate)

    async def fake_compile_files(_path, _torrent, _metadata):
        return UploadFiles(torrent_data=b"torrent")

    monkeypatch.setattr(cross_upload_module, "convert_folder", fake_convert)
    monkeypatch.setattr(cross_upload_module, "transcode_folder", fake_transcode)
    monkeypatch.setattr(cross_upload_module, "generate_torrent", lambda _site, path: (f"{path}.torrent", object()))
    monkeypatch.setattr(cross_upload_module, "compile_files", fake_compile_files)
    monkeypatch.setattr(cross_upload_module, "generate_conversion_description", lambda *_args: "16-bit description")
    monkeypatch.setattr(cross_upload_module, "generate_transcode_description", lambda _url, rate: f"{rate} description")

    class Target:
        base_url = "https://orpheus.network"

        def __init__(self):
            self.uploads = []

        async def upload(self, data, _files):
            self.uploads.append(data)
            return 100 + len(self.uploads), 9

        async def torrentgroup(self, _group_id):
            return {
                "group": {"year": 2020, "recordLabel": "Label", "catalogueNumber": "CAT-1"},
                "torrents": [],
            }

    target: Any = Target()
    original_data = {
        "title": "Album",
        "artists[]": ["Artist"],
        "importance[]": [1],
        "year": 2020,
        "releasetype": 1,
        "format": "FLAC",
        "bitrate": "24bit Lossless",
        "media": "WEB",
        "release_desc": "original",
    }

    anyio.run(
        _upload_conversions,
        tmp_path,
        original_data,
        target,
        9,
        "https://orpheus.network/torrents.php?torrentid=99",
        "WEB",
        True,
        ("V0", "320", "V0"),
    )

    assert [(upload["format"], upload["bitrate"]) for upload in target.uploads] == [
        ("FLAC", "Lossless"),
        ("MP3", "V0 (VBR)"),
        ("MP3", "320"),
    ]
    assert all(upload["groupid"] == 9 for upload in target.uploads)
    assert all("title" not in upload for upload in target.uploads)


def test_all_formats_selects_every_possible_conversion() -> None:
    assert _conversion_options(
        {"format": "FLAC", "encoding": "24bit Lossless"},
        False,
        (),
        True,
    ) == (True, ("320", "V0"))
    assert _conversion_options(
        {"format": "FLAC", "encoding": "Lossless"},
        False,
        (),
        True,
    ) == (False, ("320", "V0"))


def test_existing_group_skips_duplicate_original(tmp_path: Path, monkeypatch) -> None:
    conversion_calls = []

    async def fake_upload_conversions(*args):
        conversion_calls.append(args)

    class Target:
        base_url = "https://orpheus.network"

        async def upload(self, _data, _files):
            raise AssertionError("original torrent must not be uploaded")

    monkeypatch.setattr(cross_upload_module, "_release_path", lambda _response: tmp_path)
    monkeypatch.setattr(cross_upload_module, "_compile_data", lambda *_args: {"format": "FLAC"})
    monkeypatch.setattr(cross_upload_module, "_upload_conversions", fake_upload_conversions)

    async def run():
        return await cross_upload_module._upload_response(
            {"torrent": {"format": "FLAC", "encoding": "Lossless", "media": "WEB"}},
            cast("Any", SourceSite()),
            cast("Any", Target()),
            target_group_id=9,
            transcodes=("320", "V0"),
        )

    uploaded = anyio.run(run)
    assert uploaded == (0, 9)
    assert len(conversion_calls) == 1
    assert conversion_calls[0][3] == 9


def test_existing_conversions_are_filtered_before_processing() -> None:
    class Target:
        async def torrentgroup(self, _group_id):
            return {
                "group": {"year": 2020, "recordLabel": "Label", "catalogueNumber": "CAT-1"},
                "torrents": [
                    {
                        "media": "WEB",
                        "format": "FLAC",
                        "encoding": "Lossless",
                        "remasterYear": 2020,
                    },
                    {
                        "media": "WEB",
                        "format": "MP3",
                        "encoding": "V0 (VBR)",
                        "remasterYear": 2020,
                    },
                    {
                        "media": "CD",
                        "format": "MP3",
                        "encoding": "320",
                        "remasterYear": 2020,
                    },
                ],
            }

    data = {
        "media": "WEB",
        "year": 2020,
        "remaster_year": 2020,
        "record_label": "Label",
        "catalogue_number": "CAT-1",
    }

    missing = anyio.run(_missing_conversions, cast("Any", Target()), 9, data, True, ("V0", "320"))
    assert missing == (False, ("320",))


def test_red_images_are_rehosted_for_a_target_without_a_proxy(monkeypatch) -> None:
    cover = "https://redacted.sh/t/cover.jpg"
    inline = "https://redacted.sh/t/inline"
    calls = []

    async def fake_rehost(url, _source_site, image_host):
        calls.append((image_host, url))
        return f"https://{image_host}.example/{Path(url).name}"

    monkeypatch.setattr(cross_upload_module, "_rehost_red_image", fake_rehost)
    data = {
        "image": cover,
        "album_desc": f"[img]{cover}[/img]\n[img]{inline}[/img]",
        "release_desc": f"[img]{inline}[/img]",
    }

    result = anyio.run(
        cross_upload_module._rehost_red_images,
        data,
        cast("Any", SimpleNamespace(site_code="RED")),
        cast("Any", SimpleNamespace(site_code="DIC")),
    )

    assert all("redacted.sh/t/" not in result[field] for field in ("image", "album_desc", "release_desc"))
    assert set(calls) == {
        (cross_upload_module.cfg.image.cover_uploader, cover),
        (cross_upload_module.cfg.image.image_uploader, cover),
        (cross_upload_module.cfg.image.image_uploader, inline),
    }


def test_red_images_pass_through_to_ops(monkeypatch) -> None:
    # OPS proxies and caches RED-hosted images, so nothing is downloaded or rehosted.
    async def fake_rehost(*_args):
        raise AssertionError("no RED image may be rehosted for an OPS target")

    monkeypatch.setattr(cross_upload_module, "_rehost_red_image", fake_rehost)
    cover = "https://redacted.sh/i/cover.jpg"
    data = {
        "image": cover,
        "album_desc": f"[img]{cover}[/img]",
        "release_desc": "[img]https://redacted.sh/t/inline[/img]",
    }

    result = anyio.run(
        cross_upload_module._rehost_red_images,
        data,
        cast("Any", SimpleNamespace(site_code="RED")),
        cast("Any", SimpleNamespace(site_code="OPS")),
    )

    assert result == data


def test_red_image_urls_lose_their_credentials_on_the_way_to_ops(monkeypatch) -> None:
    # RED stores the uploader's per-viewer credentials, including their user id, in the
    # URL it hands back through the API; OPS must only ever receive the bare image URL.
    async def fake_rehost(*_args):
        raise AssertionError("no RED image may be rehosted for an OPS target")

    monkeypatch.setattr(cross_upload_module, "_rehost_red_image", fake_rehost)
    signed = "https://redacted.sh/i/cover.jpg?h=0123456789abcdefghijkl&e=1700000000&u=12345"
    data = {
        "image": signed,
        "album_desc": f"[img]{signed}[/img] [img]https://files.catbox.moe/keep.jpg?x=1[/img]",
        "release_desc": "[img]https://redacted.sh/t/thumb.jpg?h=a&e=1&u=2[/img]",
    }

    result = anyio.run(
        cross_upload_module._rehost_red_images,
        data,
        cast("Any", SimpleNamespace(site_code="RED")),
        cast("Any", SimpleNamespace(site_code="OPS")),
    )

    assert result == {
        "image": "https://redacted.sh/i/cover.jpg",
        "album_desc": "[img]https://redacted.sh/i/cover.jpg[/img] [img]https://files.catbox.moe/keep.jpg?x=1[/img]",
        "release_desc": "[img]https://redacted.sh/t/thumb.jpg[/img]",
    }


def test_verify_release_files_passes_on_exact_match(tmp_path: Path) -> None:
    (tmp_path / "01.flac").write_bytes(b"x" * 100)
    (tmp_path / "02.flac").write_bytes(b"y" * 200)
    response = {"torrent": {"fileList": "01.flac{{{100}}}|||02.flac{{{200}}}"}}
    cross_upload_module._verify_release_files(response, tmp_path)  # no raise


def test_verify_release_files_rejects_size_mismatch(tmp_path: Path) -> None:
    import asyncclick as click

    (tmp_path / "01.flac").write_bytes(b"x" * 50)  # retagged: size changed
    response = {"torrent": {"fileList": "01.flac{{{100}}}"}}
    try:
        cross_upload_module._verify_release_files(response, tmp_path)
        raise AssertionError("expected ClickException")
    except click.ClickException:
        pass


def test_verify_release_files_rejects_renamed_file(tmp_path: Path) -> None:
    import asyncclick as click

    (tmp_path / "renamed.flac").write_bytes(b"x" * 100)  # same bytes, different name
    response = {"torrent": {"fileList": "01.flac{{{100}}}"}}
    try:
        cross_upload_module._verify_release_files(response, tmp_path)
        raise AssertionError("expected ClickException")
    except click.ClickException:
        pass


class _RedImageResponse:
    status = 200
    content_type = "image/jpeg"
    content_length = 3

    def __init__(self) -> None:
        self.content = SimpleNamespace(iter_chunked=self._iter_chunked)

    async def _iter_chunked(self, _size):
        yield b"\xff\xd8\xff"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _host_returning(url):
    async def upload_file(_path):
        return url, None

    return SimpleNamespace(ImageUploader=lambda: SimpleNamespace(upload_file=upload_file))


def _red_source(fetched: list | None = None):
    def site_get(url, headers=None):
        # Stands in for BaseGazelleApi.site_get, which spends the source tracker's rate limit.
        if fetched is not None:
            fetched.append((url, headers))
        return _RedImageResponse()

    return cast("Any", SimpleNamespace(base_url="https://redacted.sh", site_get=site_get))


def _rehost(monkeypatch, returned, fetched: list | None = None):
    monkeypatch.setitem(cross_upload_module.HOSTS, "catbox", _host_returning(returned))
    return anyio.run(
        cross_upload_module._rehost_red_image, "https://redacted.sh/i/x.jpg", _red_source(fetched), "catbox"
    )


def test_a_rehosted_image_returns_its_new_url(monkeypatch) -> None:
    fetched: list = []
    rehosted = _rehost(monkeypatch, "https://files.catbox.moe/abc.jpg", fetched)
    assert rehosted == "https://files.catbox.moe/abc.jpg"
    assert fetched == [("https://redacted.sh/i/x.jpg", {"Referer": "https://redacted.sh/"})]


@pytest.mark.parametrize("returned", ["", None, "https://", "Something went wrong"])
def test_a_rehost_without_a_usable_url_is_refused(returned, monkeypatch) -> None:
    # It would otherwise be substituted into the description, and None breaks str.replace outright.
    with pytest.raises(click.ClickException):
        _rehost(monkeypatch, returned)
