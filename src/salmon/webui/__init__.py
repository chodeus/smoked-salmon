"""The salmon web interface: a long-lived server exposing salmon's tools in the browser.

Heavy imports (fastapi, uvicorn) happen inside the command so that normal CLI
startup stays fast.
"""

import asyncclick as click

from salmon.common import commandgroup


@commandgroup.command()
@click.option("--host", "-h", default="127.0.0.1", help="Host to bind the web interface to.")
@click.option("--port", "-p", default=55155, help="Port to bind the web interface to.")
@click.option("--dev", is_flag=True, help="Allow CORS from the Vite dev server (localhost:5173).")
async def web(host: str, port: int, dev: bool) -> None:
    """Start the salmon web interface."""
    import uvicorn

    from salmon.webui.app import create_app

    app = create_app(dev=dev, host=host)
    display_host = "localhost" if host in {"0.0.0.0", "127.0.0.1"} else host
    click.secho(f"salmon web interface: http://{display_host}:{port}", fg="cyan", bold=True)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()
