from os.path import dirname, join

import aiohttp_jinja2
import jinja2
from aiohttp import web
from aiohttp_jinja2 import render_template

from salmon import cfg
from salmon.web import spectrals

web_cfg = cfg.upload.web_interface


async def create_app_async(specs_path: str | None = None) -> web.AppRunner:
    """Create and start the aiohttp web application.

    Args:
        specs_path: The folder of spectral images to serve under the static URL's ``/specs``.

    Returns:
        The AppRunner instance for the web server.

    Raises:
        OSError: If the port is already in use.
    """
    app = web.Application()
    add_routes(app, specs_path)
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(join(dirname(__file__), "templates")))
    runner = web.AppRunner(app)
    await runner.setup()
    # This viewer serves spectral images without authentication, so it always binds loopback.
    # View it locally, over an SSH tunnel, or use the authenticated `salmon web` interface.
    site = web.TCPSite(runner, "127.0.0.1", web_cfg.port)
    await site.start()
    return runner


def add_routes(app: web.Application, specs_path: str | None = None) -> None:
    """Add routes to the web application.

    Args:
        app: The aiohttp web application.
        specs_path: The folder of spectral images to serve under the static URL's ``/specs``.
    """
    # Served from their own folder at the URL the templates use; never link them into the installed package.
    if specs_path is not None:
        app.router.add_static("/static/specs", specs_path)
    # The package's own files: an install that links them (UV_LINK_MODE=symlink) still serves them.
    app.router.add_static("/static", join(dirname(__file__), "static"), follow_symlinks=True)
    app.router.add_route("GET", "/", handle_index)
    app.router.add_route("GET", "/spectrals", spectrals.handle_spectrals)
    app[aiohttp_jinja2.static_root_key] = web_cfg.static_root_url


async def handle_index(request: web.Request) -> web.Response:
    """Handle the index page request.

    Args:
        request: The aiohttp request object.

    Returns:
        The rendered index page response.
    """
    return render_template("index.html", request, {})
