from os.path import dirname, join

import aiohttp_jinja2
import asyncclick as click
import jinja2
from aiohttp import web
from aiohttp_jinja2 import render_template

from salmon import cfg
from salmon.web import spectrals

web_cfg = cfg.upload.web_interface


async def create_app_async() -> web.AppRunner:
    """Create and start the aiohttp web application.

    Returns:
        The AppRunner instance for the web server.

    Raises:
        OSError: If the port is already in use.
    """
    app = web.Application()
    add_routes(app)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    runner = web.AppRunner(app)
    await runner.setup()
    # The spectral viewer serves images without authentication. Don't expose it on a
    # non-loopback address unless a web auth_token is configured — bind loopback instead.
    host = web_cfg.host
    if host not in ("127.0.0.1", "localhost", "::1", "") and not getattr(web_cfg, "auth_token", None):
        click.secho(
            f"Spectral viewer: binding 127.0.0.1 instead of {host} (unauthenticated; set "
            "[upload.web_interface] auth_token to serve it on the LAN).",
            fg="yellow",
        )
        host = "127.0.0.1"
    site = web.TCPSite(runner, host, web_cfg.port)
    await site.start()
    return runner


def add_routes(app: web.Application) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
    """
    app.router.add_static("/static", join(dirname(__file__), "static"), follow_symlinks=True)
    app.router.add_route("GET", "/", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app["static_root_url"] = web_cfg.static_root_url


async def handle_index(request: web.Request) -> web.Response:
    """Handle the index page request.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered index page response.
    """
    return render_template("index.html", request, {})
