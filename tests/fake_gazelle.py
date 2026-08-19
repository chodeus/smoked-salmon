"""A minimal fake Gazelle tracker for testing the upload pipeline end to end.

Implements just enough of the ajax.php surface for salmon's upload flow to run
through the real aiohttp / cookie / retry layer without a real tracker:

    GET  /ajax.php?action=index        -> authkey / passkey
    GET  /ajax.php?action=browse       -> dupe search (configurable results)
    GET  /ajax.php?action=requests     -> request search (configurable results)
    GET  /ajax.php?action=torrentgroup -> group info (for post-upload printing)
    GET  /log.php                      -> empty recent-upload log
    POST /ajax.php?action=upload       -> success with torrent/group ids

Recorded uploads are available on the app under ``app[UPLOADS]``.
"""

from __future__ import annotations

from aiohttp import web

UPLOADS: web.AppKey[list] = web.AppKey("uploads", list)
BROWSE_RESULTS: web.AppKey[list] = web.AppKey("browse_results", list)
REQUEST_RESULTS: web.AppKey[list] = web.AppKey("request_results", list)
TORRENT_ID: web.AppKey[int] = web.AppKey("torrent_id", int)
GROUP_ID: web.AppKey[int] = web.AppKey("group_id", int)


def make_fake_gazelle(
    *,
    browse_results: list[dict] | None = None,
    request_results: list[dict] | None = None,
    torrent_id: int = 424242,
    group_id: int = 131313,
) -> web.Application:
    app = web.Application()
    app[UPLOADS] = []
    app[BROWSE_RESULTS] = browse_results or []
    app[REQUEST_RESULTS] = request_results or []
    app[TORRENT_ID] = torrent_id
    app[GROUP_ID] = group_id

    def ok(response: dict) -> web.Response:
        return web.json_response({"status": "success", "response": response})

    async def ajax(request: web.Request) -> web.Response:
        action = request.query.get("action")
        if action == "index":
            return ok({"authkey": "fake-authkey", "passkey": "fake-passkey", "id": 1})
        if action == "browse":
            return ok({"results": request.app[BROWSE_RESULTS]})
        if action == "requests":
            return ok({"results": request.app[REQUEST_RESULTS]})
        if action == "torrentgroup":
            gid = int(request.query.get("id", request.app[GROUP_ID]))
            return ok(
                {
                    "group": {
                        "id": gid,
                        "name": "Testalbum",
                        "year": 2024,
                        "musicInfo": {"artists": [{"id": 1, "name": "Testartist"}]},
                        "recordLabel": "",
                        "catalogueNumber": "",
                    },
                    "torrents": [
                        {
                            "id": request.app[TORRENT_ID],
                            "format": "FLAC",
                            "encoding": "Lossless",
                            "media": "WEB",
                            "remastered": False,
                        }
                    ],
                }
            )
        return web.json_response({"status": "failure", "error": f"unknown action {action}"}, status=400)

    async def upload(request: web.Request) -> web.Response:
        reader = await request.multipart()
        fields: dict[str, str] = {}
        files: list[str] = []
        async for part in reader:
            if part.filename:
                files.append(part.filename)
                await part.read()  # drain
            else:
                fields[part.name] = (await part.read()).decode("utf-8", "replace")
        request.app[UPLOADS].append({"fields": fields, "files": files})
        return ok({"torrentid": request.app[TORRENT_ID], "groupid": request.app[GROUP_ID]})

    async def dispatch_ajax(request: web.Request) -> web.Response:
        if request.method == "POST" and request.query.get("action") == "upload":
            return await upload(request)
        return await ajax(request)

    async def log_php(_request: web.Request) -> web.Response:
        return web.Response(text="<html><body></body></html>", content_type="text/html")

    async def dump_uploads(request: web.Request) -> web.Response:
        return web.json_response(request.app[UPLOADS])

    app.router.add_route("*", "/ajax.php", dispatch_ajax)
    app.router.add_get("/log.php", log_php)
    app.router.add_get("/_uploads", dump_uploads)
    return app
