import ipaddress
import math
from datetime import UTC
from email.utils import parsedate_to_datetime
from time import time

from yarl import URL

_NON_PUBLIC_IP_ATTRS = ("is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified")


def http_url_hostname(value: str | None) -> str | None:
    """Hostname of ``value`` when it is an http(s) URL, else None."""
    try:
        parsed = URL(value or "")
    except (TypeError, ValueError):
        # TypeError: yarl refuses a non-str, which a host's JSON field can still be.
        return None
    return parsed.host if parsed.scheme in {"http", "https"} else None


def is_http_url(value: str | None) -> bool:
    """True when ``value`` is an http(s) URL with a hostname."""
    return http_url_hostname(value) is not None


def is_public_ip(address: str) -> bool:
    """True only for a globally routable address, so CGNAT and every special range are refused."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # Both: is_global refuses CGNAT, which the denylist allows; the denylist refuses NAT64,
    # which is_global allows.
    return ip.is_global and not any(getattr(ip, attr) for attr in _NON_PUBLIC_IP_ATTRS)


def parse_retry_after(value: str | None) -> float | None:
    """Get the wait in seconds from a Retry-After header (delay-seconds or HTTP-date)."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        # A negative delay is invalid (RFC 9110), so the caller's fallback applies.
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # a "-0000" date: UTC, not local time
        when = when.replace(tzinfo=UTC)
    wait = when.timestamp() - time()
    # A date already past names no wait, so the normal backoff applies.
    return wait if wait > 0 else None
