"""Development launcher: salmon web with RED pointed at a local fake Gazelle.

Starts the fake tracker from tests/fake_gazelle.py on 127.0.0.1:55166, rewires
the RedApi URLs and credentials to it, then serves the web interface exactly
like `salmon web`. Nothing here talks to a real tracker.

Run from the repo root:

    uv run python tools/dev_web_with_fake_tracker.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

FAKE_PORT = 55166
WEB_PORT = 55155


async def main() -> None:
    import uvicorn
    from aiohttp import web as aioweb
    from fake_gazelle import make_fake_gazelle

    from salmon.trackers.red import RedApi
    from salmon.webui.app import create_app

    fake = make_fake_gazelle(
        browse_results=[
            {
                "groupId": 900001,
                "groupName": "Testalbum (older rip)",
                "artist": "Testartist",
                "groupYear": 2020,
                "groupTime": "1700000000",
                "torrents": [{"id": 1, "media": "CD", "format": "FLAC", "encoding": "Lossless", "remastered": False}],
            }
        ]
    )
    runner = aioweb.AppRunner(fake)
    await runner.setup()
    await aioweb.TCPSite(runner, "127.0.0.1", FAKE_PORT).start()
    print(f"fake gazelle: http://127.0.0.1:{FAKE_PORT}")

    RedApi.base_url = f"http://127.0.0.1:{FAKE_PORT}"
    RedApi.tracker_url = f"http://127.0.0.1:{FAKE_PORT}"

    original_init = RedApi.__init__

    def patched_init(self):
        original_init(self)
        self.base_url = f"http://127.0.0.1:{FAKE_PORT}"
        self.tracker_url = f"http://127.0.0.1:{FAKE_PORT}"
        self.cookie = "fake-session"
        self.api_key = "fake-api-key"

    RedApi.__init__ = patched_init

    app = create_app(host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=WEB_PORT, log_level="warning")
    print(f"salmon web (fake tracker mode): http://localhost:{WEB_PORT}")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
