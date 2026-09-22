import ipaddress

from yarl import URL

_NON_PUBLIC_IP_ATTRS = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")


def http_url_hostname(value: str | None) -> str | None:
    """Hostname of ``value`` when it is an http(s) URL, else None."""
    try:
        parsed = URL(value or "")
    except ValueError:
        return None
    return parsed.host if parsed.scheme in {"http", "https"} else None


def is_http_url(value: str | None) -> bool:
    """True when ``value`` is an http(s) URL with a hostname."""
    return http_url_hostname(value) is not None


def is_public_ip(address: str) -> bool:
    """False for loopback, private, link-local, reserved, multicast and unspecified addresses."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not any(getattr(ip, attr) for attr in _NON_PUBLIC_IP_ATTRS)
