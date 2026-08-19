import salmon.trackers.red as red_mod
from salmon.trackers.base import BaseGazelleApi, HttpResponse


class _Files:
    log_files: list = []
    torrent_data = b"x"


async def test_base_dry_run_upload_returns_zero_and_no_network():
    api = BaseGazelleApi.__new__(BaseGazelleApi)
    api.site_string = "OPS"
    assert await api.dry_run_upload({"title": "x"}, _Files()) == (0, 0)


async def test_red_dry_run_sends_dryrun_flag_and_no_real_upload(monkeypatch):
    api = red_mod.RedApi.__new__(red_mod.RedApi)
    api.api_key = "k"
    api.authkey = "auth"
    api.base_url = "https://redacted.sh"
    api.site_string = "RED"

    captured: dict = {}
    monkeypatch.setattr(red_mod, "_compose_form_data", lambda files, data: captured.update(data) or "FORM")

    async def fake_ensure():
        pass

    posted = {"url": None}

    async def fake_request(method, url, data=None, **kwargs):
        posted["url"] = url
        return HttpResponse(text='{"status":"success","response":{}}', url=url, status=200)

    monkeypatch.setattr(api, "ensure_authenticated", fake_ensure)
    monkeypatch.setattr(api, "_request", fake_request)

    result = await api.dry_run_upload({"title": "x"}, _Files())
    assert captured.get("dryrun") is True          # RED's dryrun flag was set
    assert "action=upload" in posted["url"]         # it hit the upload endpoint (in dryrun mode)
    assert result == (0, 0)                          # no torrent id returned


async def test_red_dry_run_without_api_key_makes_no_request(monkeypatch):
    api = red_mod.RedApi.__new__(red_mod.RedApi)
    api.api_key = ""
    api.site_string = "RED"

    called = {"request": False}

    async def fake_request(*args, **kwargs):
        called["request"] = True
        return None

    monkeypatch.setattr(api, "_request", fake_request)
    assert await api.dry_run_upload({"title": "x"}, _Files()) == (0, 0)
    assert called["request"] is False               # no api_key -> local-only, never posts
