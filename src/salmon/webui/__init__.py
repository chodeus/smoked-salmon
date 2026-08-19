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

    from salmon.webui.app import LOOPBACK_HOSTS, create_app
    from salmon.webui.auth import ENV_VAR, resolve_auth_token

    token = resolve_auth_token()
    if host not in LOOPBACK_HOSTS and not token:
        # The UI can upload and delete with the account's cookies; never open on a LAN.
        raise click.ClickException(
            f"Refusing to serve on {host} without an auth token. Set the {ENV_VAR} env var or "
            "[upload.web_interface] auth_token in your config, or bind to 127.0.0.1 for local-only use."
        )

    app = create_app(dev=dev, host=host, auth_token=token)
    display_host = "localhost" if host in {"0.0.0.0", "127.0.0.1"} else host
    click.secho(f"salmon web interface: http://{display_host}:{port}", fg="cyan", bold=True)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()
