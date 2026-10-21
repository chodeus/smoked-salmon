"""Upstream's tests of the ``salmon web`` token and the ``[web]`` section, until its whole server comes over."""

import re
from functools import partial
from pathlib import Path

import anyio
import asyncclick as click
import msgspec
import pytest
from test_config_unknown_keys import _DEFAULT_CONFIG, _template  # pyright: ignore[reportMissingImports]

from salmon import cfg
from salmon.config import _parse_config
from salmon.webui.auth import ENV_VAR, resolve_token

# --- Starting the server ------------------------------------------------------


@pytest.fixture
def no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(cfg.web, "token", None)


@pytest.mark.usefixtures("no_token")
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "seedbox.lan"])  # nosec B104
def test_a_non_loopback_bind_without_a_token_refuses_to_start(host: str) -> None:
    with pytest.raises(click.ClickException, match="will not listen on .* without a token"):
        resolve_token(host, None)


@pytest.mark.usefixtures("no_token")
def test_the_command_refuses_to_start_on_all_interfaces_without_a_token() -> None:
    from asyncclick.testing import CliRunner

    from salmon.webui import web as web_command

    result = anyio.run(partial(CliRunner().invoke, web_command, ["--host", "0.0.0.0", "--port", "55155"]))  # nosec B104
    assert result.exit_code == 1
    assert "will not listen on 0.0.0.0 without a token" in result.output


@pytest.mark.usefixtures("no_token")
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_without_a_token_gets_a_new_one_each_start(host: str) -> None:
    first, generated = resolve_token(host, None)
    second, _ = resolve_token(host, None)
    assert generated
    assert len(first) >= 32
    assert first != second


def test_the_environment_beats_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "e" * 32)
    assert resolve_token("0.0.0.0", "c" * 32) == ("e" * 32, False)  # nosec B104
    monkeypatch.delenv(ENV_VAR)
    assert resolve_token("0.0.0.0", "c" * 32) == ("c" * 32, False)  # nosec B104


def test_a_short_environment_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "e" * 31)
    with pytest.raises(click.ClickException, match="at least 32"):
        resolve_token("127.0.0.1", None)


# --- The [web] section -------------------------------------------------------


def _parse(tmp_path: Path, section: str):
    path = tmp_path / "config.toml"
    path.write_text(_template(tmp_path) + "\n" + section, encoding="utf-8")
    return _parse_config(path).web


def test_a_config_without_web_gets_the_defaults(tmp_path: Path) -> None:
    web_cfg = _parse(tmp_path, "")
    assert (web_cfg.host, web_cfg.port, web_cfg.token, web_cfg.allowed_hosts, web_cfg.max_jobs) == (
        "127.0.0.1",
        55155,
        None,
        [],
        2,
    )
    # The spectrals viewer keeps its own section.
    assert cfg.upload.web_interface.port == 55110


def test_the_documented_web_section_is_valid(tmp_path: Path) -> None:
    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    section = text[text.index("\n# [web]\n") :]
    uncommented = "\n".join(
        line.removeprefix("# ") for line in section.splitlines() if re.match(r"# (\w+ = |\[web\])", line)
    )
    web_cfg = _parse(tmp_path, uncommented)
    assert web_cfg.port == 55155
    assert web_cfg.allowed_hosts == ["seedbox.lan", "192.168.1.20"]
    assert web_cfg.token is not None and len(web_cfg.token) >= 32


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[web]\ntoken = "' + "x" * 31 + '"\n', "web.token must be at least 32 characters"),
        ("[web]\nmax_jobs = 0\n", "web.max_jobs must be at least 1"),
        ("[web]\nport = 0\n", "web.port must be between 1 and 65535"),
        ("[web]\nport = 65536\n", "web.port must be between 1 and 65535"),
    ],
)
def test_invalid_web_settings_are_refused(tmp_path: Path, section: str, message: str) -> None:
    with pytest.raises(msgspec.ValidationError, match=message):
        _parse(tmp_path, section)


def test_an_empty_token_is_no_token(tmp_path: Path) -> None:
    assert _parse(tmp_path, '[web]\ntoken = ""\n').token is None
