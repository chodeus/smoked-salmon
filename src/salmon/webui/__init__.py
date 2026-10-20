"""The salmon web interface: a long-lived server exposing salmon's tools in the browser.

Heavy imports (fastapi, uvicorn) happen inside the command so that normal CLI
startup stays fast.
"""

import ipaddress

import asyncclick as click

from salmon import cfg
from salmon.common import commandgroup


@commandgroup.command()
@click.option("--host", "-h", default="127.0.0.1", help="Host to bind the web interface to.")
@click.option("--port", "-p", default=55155, help="Port to bind the web interface to.")
@click.option("--dev", is_flag=True, help="Allow CORS from the Vite dev server (localhost:5173).")
async def web(host: str, port: int, dev: bool) -> None:
    """Start the salmon web interface."""
    import secrets

    import uvicorn

    from salmon.webui.app import LOOPBACK_HOSTS, create_app
    from salmon.webui.auth import ENV_VAR, resolve_auth_token

    token = resolve_auth_token()
    made = not token
    if made and host not in LOOPBACK_HOSTS:
        # The UI can upload and delete with the account's cookies; never open on a LAN.
        raise click.ClickException(
            f"Refusing to serve on {host} without an auth token. Set the {ENV_VAR} env var or "
            "[upload.web_interface] auth_token in your config, or bind to 127.0.0.1 for local-only use."
        )
    if made:
        # Any local user, and any page the browser opens, can reach loopback: it gets a token too.
        token = secrets.token_urlsafe(32)

    app = create_app(dev=dev, host=host, auth_token=token)
    if _names_no_machine(host) and not cfg.upload.web_interface.allowed_hosts:
        click.secho(
            "No allowed_hosts are set under [upload.web_interface]: a browser on another machine is refused "
            '("This host name is not allowed") until they list the names you open salmon web by.',
            fg="yellow",
        )
    display_host = "localhost" if host in {"0.0.0.0", "127.0.0.1"} else host
    url = f"http://{display_host}:{port}"
    click.secho(f"salmon web interface: {url}", fg="cyan", bold=True)
    if made:
        # After the #, which a browser never sends: the page logs in with it, then drops it from the address bar.
        click.secho(f"No auth token is set, so this run made one. Log in with: {url}/#token={token}", fg="yellow")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


def _names_no_machine(host: str) -> bool:
    """Whether a bind address is 0.0.0.0 or ::, which a browser cannot open salmon web by."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_unspecified
    except ValueError:
        return False
