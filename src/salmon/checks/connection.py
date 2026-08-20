"""Tracker connectivity checks shared by `salmon checkconf` and the web interface."""

import salmon.trackers
from salmon.trackers.base import _safe_response_excerpt


async def check_tracker_connection(code: str) -> dict:
    """Test one tracker's session cookie and, when configured, its API key.

    Error text is passed through the tracker redactor: a failed request can echo
    an HTML page carrying authkey/passkey, and this result is returned over HTTP.
    """
    tracker = salmon.trackers.get_class(code)()
    index_url = f"{tracker.base_url}/ajax.php"
    result: dict = {
        "tracker": code,
        "session_ok": False,
        "session_error": None,
        "api_key_configured": bool(tracker.api_key),
        "api_key_ok": None,
        "api_key_error": None,
    }

    try:
        await tracker._request("GET", index_url, params={"action": "index"}, prefer_api_key=False)
        result["session_ok"] = True
    except Exception as error:
        result["session_error"] = _safe_response_excerpt(str(error), limit=300)

    if tracker.api_key:
        try:
            await tracker._request("GET", index_url, params={"action": "index"}, prefer_api_key=True)
            result["api_key_ok"] = True
        except Exception as error:
            result["api_key_ok"] = False
            result["api_key_error"] = _safe_response_excerpt(str(error), limit=300)

    return result
