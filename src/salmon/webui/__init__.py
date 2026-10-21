"""The salmon web interface: a long-lived server exposing salmon's tools in the browser.

Heavy imports (fastapi, uvicorn) happen inside the command so that normal CLI
startup stays fast.
"""

import ipaddress

import asyncclick as click

from salmon import cfg
from salmon.common import commandgroup


@commandgroup.command()
@click.option("--host", default=None, help="Address to listen on [default: [web] host, else 127.0.0.1]")
@click.option(
    "--port", type=click.IntRange(1, 65535), default=None, help="Port to listen on [default: [web] port, else 55155]"
)
@click.option("--dev", is_flag=True, help="Accept requests from the Vite dev server (http://localhost:5173)")
async def web(host: str | None, port: int | None, dev: bool) -> None:
    """Start the web interface"""
    import uvicorn

    from salmon.webui.app import create_app
    from salmon.webui.auth import resolve_token

    host = host or cfg.web.host
    port = port or cfg.web.port
    token, made = resolve_token(host, cfg.web.token)
    app = create_app(dev=dev, host=host, auth_token=token)
    if _names_no_machine(host) and not cfg.web.allowed_hosts:
        click.secho(
            "No allowed_hosts are set under [web]: a browser on another machine is refused "
            '("This host name is not allowed") until they list the names you open salmon web by.',
            fg="yellow",
        )
    display_host = "localhost" if host in {"0.0.0.0", "::", "127.0.0.1"} else host
    url = _url(display_host, port)
    click.secho(f"salmon web interface: {url}", fg="cyan", bold=True)
    if made:
        # After the #, which a browser never sends: the page logs in with it, then drops it from the address bar.
        click.secho(f"No auth token is set, so this run made one. Log in with: {url}#token={token}", fg="yellow")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


def _names_no_machine(host: str) -> bool:
    """Whether a bind address is 0.0.0.0 or ::, which a browser cannot open salmon web by."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_unspecified
    except ValueError:
        return False
