import asyncio

from salmon.common import handle_scrape_errors
from salmon.errors import ScrapeError
from salmon.webui import output


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
    messages: list[tuple[str, dict]] = []

    def fake_secho(message: str, **kwargs) -> None:
        messages.append((message, kwargs))

    async def raise_error() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("salmon.common.click.secho", fake_secho)

    with output.to(lambda _text, _err: None):
        result = asyncio.run(handle_scrape_errors(raise_error()))

    assert result is None
    assert messages == [("Unexpected scrape error: boom", {"fg": "red", "bold": True})]
    assert "Traceback" in capsys.readouterr().err
