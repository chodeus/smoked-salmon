import os
import sys
import time
from collections.abc import Iterator
from email.utils import formatdate
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from salmon.common.urls import http_url_hostname, is_http_url, parse_retry_after


@pytest.fixture
def far_from_utc() -> Iterator[None]:
    """Local time 9 hours ahead of UTC, so a date without a zone read as local time is 9 hours off."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is Unix only")
    before = os.environ.get("TZ")
    os.environ["TZ"] = "JST-9"
    time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


@pytest.mark.parametrize(("value", "seconds"), [("120", 120.0), ("0", 0.0), ("2.5", 2.5)])
def test_delay_seconds(value: str, seconds: float) -> None:
    assert parse_retry_after(value) == seconds


@pytest.mark.parametrize("value", [None, "", "soon", "nan", "inf", "-inf", "1e400", "-5"])
def test_no_valid_wait_is_none(value: str | None) -> None:
    assert parse_retry_after(value) is None


@pytest.mark.parametrize("usegmt", [True, False], ids=["gmt", "naive"])
@pytest.mark.usefixtures("far_from_utc")
def test_http_date_is_the_wait_until_then_in_utc(usegmt: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_800_000_000.0
    # A fixed clock: formatdate drops the fraction of a second, so a stalled run would fall short of a bound.
    monkeypatch.setattr("salmon.common.urls.time", lambda: now)
    assert parse_retry_after(formatdate(now + 30, usegmt=usegmt)) == 30


def test_http_date_already_past_is_none() -> None:
    assert parse_retry_after(formatdate(time.time() - 30, usegmt=True)) is None


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("https://files.catbox.moe/abc.jpg", "files.catbox.moe"),
        ("http://example.com:8080/x", "example.com"),
        ("https://user:pw@example.com/x", "example.com"),
        ("http://[::1]/", "::1"),
    ],
)
def test_a_usable_url_yields_its_hostname(value, host) -> None:
    assert http_url_hostname(value) == host


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "https://",
        "https:///abc.jpg",
        "//example.com/x",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "Something went wrong, please try again later.",
        # yarl raises on these; a parse failure must read as "no hostname", never propagate.
        "http://[::1",
        "https://example.com:notaport/x",
        # yarl raises TypeError, not ValueError, for anything that is not a str - and a host's
        # JSON url field can be a number.
        123,
        4.5,
        b"https://files.catbox.moe/a.jpg",
        ["https://files.catbox.moe/a.jpg"],
    ],
)
def test_an_unusable_value_has_no_hostname(value) -> None:
    assert http_url_hostname(value) is None


def test_is_http_url_tracks_the_hostname_lookup() -> None:
    usable = is_http_url("https://example.com/x")
    unusable = is_http_url("https://")
    assert usable is True
    assert unusable is False
