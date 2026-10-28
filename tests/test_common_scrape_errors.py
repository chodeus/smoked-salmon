import asyncio

from salmon.common import handle_scrape_errors
from salmon.errors import ScrapeError
from salmon.trackers import base as tracker_base
from salmon.webui import egress, output


def test_scrape_error_uses_message_as_exception_text() -> None:
    error = ScrapeError("Could not find Next.js data script tag", payload={"source": "Beatport"})

    assert str(error) == "Could not find Next.js data script tag"
    assert error.payload == {"source": "Beatport"}


def test_handle_scrape_errors_logs_expected_failures_without_traceback(monkeypatch) -> None:
    messages: list[tuple[str, dict]] = []

    def fake_secho(message: str, **kwargs) -> None:
        messages.append((message, kwargs))

    async def raise_error() -> None:
        raise ScrapeError("Could not find Next.js data script tag")

    monkeypatch.setattr("salmon.common.click.secho", fake_secho)

    result = asyncio.run(handle_scrape_errors(raise_error()))

    assert result is None
    assert messages == [("Scrape error: Could not find Next.js data script tag", {"fg": "red", "bold": True})]


def test_handle_scrape_errors_keeps_traceback_for_unexpected_failures(monkeypatch) -> None:
    messages: list[tuple[str, dict]] = []

    def fake_secho(message: str, **kwargs) -> None:
        messages.append((message, kwargs))

    async def raise_error() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("salmon.common.click.secho", fake_secho)

    result = asyncio.run(handle_scrape_errors(raise_error()))

    assert result is None
    assert len(messages) == 1
    assert messages[0][1] == {"fg": "red", "bold": True}
    assert messages[0][0].startswith("Unexpected scrape error: boom\nTraceback")


def test_in_a_web_job_an_unexpected_failure_keeps_its_traceback_out_of_the_log(monkeypatch, capsys) -> None:
    secret = "scrape-token-0123456789"
    monkeypatch.setattr(egress, "_configured_secrets", lambda: (secret,))
    monkeypatch.setattr(tracker_base, "_learned_secrets", set())
    logged: list[str] = []

    async def raise_error() -> None:
        raise RuntimeError(f"GET https://api.example/?token={secret} failed")

    with output.capturing(), output.to(lambda text, _err: logged.append(text)):
        result = asyncio.run(handle_scrape_errors(raise_error()))

    log = "".join(logged)
    server_stderr = capsys.readouterr().err
    assert result is None
    assert "Unexpected scrape error" in log
    assert "Traceback" not in log
    # The traceback goes to the server's own stderr, masked.
    assert "Traceback" in server_stderr
    assert secret not in server_stderr
