"""An upload whose answer is lost is looked up by its infohash, after a wait and once more if not found yet."""

import asyncio
import gc
import signal
from pathlib import Path

import anyio
import pytest
from aiohttp import web
from tenacity import wait_none
from torf import Torrent

from salmon.common import UploadFiles
from salmon.errors import LoginError, RequestError, UnknownOutcomeError
from salmon.trackers import base
from salmon.trackers.account import SlidingWindowLimiter
from salmon.trackers.base import BaseGazelleApi, RetryableError


class FakeApi(BaseGazelleApi):
    site_code = "RED"
    site_string = "RED"
    cookie = "a-cookie"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key
        super().__init__()
        self._authenticated = True


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry without waiting, so the tests count attempts, not seconds."""
    monkeypatch.setattr(BaseGazelleApi._send.retry, "wait", wait_none())  # type: ignore[attr-defined]
    monkeypatch.setattr(BaseGazelleApi, "_rate_limiter", SlidingWindowLimiter(100, 1))


def _wait_before_lookups(monkeypatch: pytest.MonkeyPatch, first: float, second: float) -> None:
    """Set how long a lost upload is waited for before its first and its second lookup."""
    monkeypatch.setattr(base, "_LOST_UPLOAD_FIRST_WAIT", first)
    monkeypatch.setattr(base, "_LOST_UPLOAD_SECOND_WAIT", second)


async def _serve(**handlers) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_route("*", f"/{path}.php", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"


async def _drop(request: web.Request) -> web.Response:
    """Close the connection instead of answering, as when the answer is lost on the way back."""
    assert request.transport is not None
    request.transport.close()
    return web.Response()


def _found(torrent_id: int, group_id: int) -> web.Response:
    return web.json_response(
        {"status": "success", "response": {"group": {"id": group_id}, "torrent": {"id": torrent_id}}}
    )


def _not_found() -> web.Response:
    return web.json_response({"status": "failure", "error": "bad parameters"})


def _upload_files(tmp_path: Path) -> tuple[UploadFiles, str]:
    album = tmp_path / "album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"not really audio" * 64)
    torrent = Torrent(album, trackers=["http://tracker.invalid/announce"], private=True, source="RED")
    torrent.generate()
    return UploadFiles(torrent_data=torrent.dump()), torrent.infohash.upper()


async def _lost_upload_found_by_infohash(tmp_path: Path) -> None:
    files, infohash = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _found(9, 5)

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        uploaded = await api.upload({"type": 0}, files)
        assert uploaded == (9, 5)
        assert posts == ["upload"]
        assert lookups == [infohash]
    finally:
        await api.close()
        await runner.cleanup()


async def _lost_upload_not_found_says_it_may_have_gone_through(tmp_path: Path) -> None:
    files, _ = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _not_found()

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        with pytest.raises(
            UnknownOutcomeError,
            match=r"did not confirm it \(bad parameters\)\. The upload may still have gone through: check your uploads",
        ):
            await api.upload({"type": 0}, files)
        assert posts == ["upload"]
        # Looked up once more after the second wait, and no more.
        assert len(lookups) == 2
    finally:
        await api.close()
        await runner.cleanup()


# Each wait before a lookup, in the tests that time them. The torrent appears half a wait away from
# any lookup, so these tests hold as long as each lookup goes out less than half a wait late.
WAIT = 0.4


async def _upload_whose_torrent_appears_later(tmp_path: Path, appears_after: float) -> tuple[list[str], list[float]]:
    """Upload to a tracker that loses the answer; return its uploads and each lookup's seconds after the upload."""
    files, infohash = _upload_files(tmp_path)
    posts: list[str] = []
    lookups: list[float] = []
    uploaded_at = 0.0

    async def ajax(request: web.Request) -> web.Response:
        nonlocal uploaded_at
        now = asyncio.get_running_loop().time()
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            uploaded_at = now
            return await _drop(request)
        assert request.query["hash"] == infohash
        lookups.append(now - uploaded_at)
        return _found(9, 5) if now - uploaded_at >= appears_after else _not_found()

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        uploaded = await api.upload({"type": 0}, files)
        assert uploaded == (9, 5)
        return posts, lookups
    finally:
        await api.close()
        await runner.cleanup()


async def _lost_upload_lookup_that_fails_otherwise_is_not_repeated(
    tmp_path: Path, status: int, raised: type[RequestError]
) -> None:
    files, _ = _upload_files(tmp_path)
    posts, lookups = [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            return await _drop(request)
        lookups.append(request.query["hash"])
        return web.json_response({"status": "failure", "error": "no"}, status=status)

    runner, url = await _serve(ajax=ajax)
    api = FakeApi(url, api_key="an-api-key")
    try:
        with pytest.raises(UnknownOutcomeError, match="may still have gone through") as caught:
            await api.upload({"type": 0}, files)
        assert isinstance(caught.value.__cause__, raised)
        # The lookup failed, it did not come back empty.
        assert "did not confirm it" in str(caught.value)
        assert posts == ["upload"]
        # Only a tracker saying it does not have the torrent is worth asking again later.
        assert len(lookups) == (5 if raised is RetryableError else 1)
    finally:
        await api.close()
        await runner.cleanup()


def test_lost_upload_found_by_infohash(tmp_path: Path) -> None:
    anyio.run(_lost_upload_found_by_infohash, tmp_path)


def test_lost_upload_not_found_says_it_may_have_gone_through(tmp_path: Path) -> None:
    anyio.run(_lost_upload_not_found_says_it_may_have_gone_through, tmp_path)


def test_lost_upload_that_appears_during_the_first_wait_is_found_on_the_first_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wait_before_lookups(monkeypatch, WAIT, WAIT)
    posts, lookups = anyio.run(_upload_whose_torrent_appears_later, tmp_path, WAIT / 2)
    assert posts == ["upload"]
    assert len(lookups) == 1
    assert lookups[0] >= WAIT


def test_lost_upload_that_appears_during_the_second_wait_is_found_on_the_second_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wait_before_lookups(monkeypatch, WAIT, WAIT)
    posts, lookups = anyio.run(_upload_whose_torrent_appears_later, tmp_path, WAIT * 1.5)
    assert posts == ["upload"]
    assert len(lookups) == 2
    assert lookups[0] >= WAIT
    assert lookups[1] >= WAIT * 2


@pytest.mark.parametrize(("status", "raised"), [(503, RetryableError), (401, LoginError)])
def test_lost_upload_lookup_that_fails_otherwise_is_not_repeated(
    tmp_path: Path, status: int, raised: type[RequestError]
) -> None:
    anyio.run(_lost_upload_lookup_that_fails_otherwise_is_not_repeated, tmp_path, status, raised)


def test_ctrl_c_while_waiting_for_a_lost_upload_sends_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wait_before_lookups(monkeypatch, 30, 30)
    files, _ = _upload_files(tmp_path)
    posts, lookups, unclosed = [], [], []

    async def ajax(request: web.Request) -> web.Response:
        if request.method == "POST":
            await request.read()
            posts.append(request.query["action"])
            # The user presses Ctrl-C a moment later, once salmon waits to look the upload up.
            asyncio.get_running_loop().call_later(0.5, signal.raise_signal, signal.SIGINT)
            return await _drop(request)
        lookups.append(request.query["hash"])
        return _found(9, 5)

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unclosed.append(context["message"]))
        runner, url = await _serve(ajax=ajax)
        api = FakeApi(url, api_key="an-api-key")
        try:
            with anyio.fail_after(5):
                await api.upload({"type": 0}, files)
        finally:
            await api.close()
            await runner.cleanup()
            # A little longer, for a lookup sent anyway to arrive.
            await anyio.sleep(0.2)
            del runner, api
            # An unclosed session or connector is only reported once it is collected.
            gc.collect()
            await anyio.sleep(0.05)

    # Ctrl-C as in a terminal, also when the tests were started in the background, where it is ignored.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        # As in salmon itself: anyio.run turns Ctrl-C into cancelling the run, then raises KeyboardInterrupt.
        with pytest.raises(KeyboardInterrupt):
            anyio.run(main)
    finally:
        signal.signal(signal.SIGINT, previous)
    out = capsys.readouterr().out
    assert "Waiting 30 s for RED to finish processing it" in out
    assert "may still have gone through: check your uploads on RED" in out
    assert posts == ["upload"]
    assert lookups == []
    assert unclosed == []
