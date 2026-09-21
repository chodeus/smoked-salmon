from yarl import URL


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
