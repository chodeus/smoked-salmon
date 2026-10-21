"""Old config keys (salmon.config.compat, ADR 0005).

Each test writes a config in its tmp dir and loads it through ``salmon.config._parse_config``, the path a user's
config goes through.
"""

from pathlib import Path
from typing import Any

import msgspec
import pytest

import salmon.release_notification
from salmon.config import _parse_config, compat
from salmon.config.validations import Cfg

UPGRADE_PAGE = "https://github.com/smokin-salmon/smoked-salmon/wiki/Upgrading-to-0.21"

# Each old key with a value that is not its default, and where the value now lands.
MOVES = [
    ("upload.web_interface.host", "upload.spectrals_viewer.host", "127.0.0.2"),
    ("upload.web_interface.port", "upload.spectrals_viewer.port", 55111),
    ("upload.web_interface.display_host", "upload.spectrals_viewer.display_host", "spectrals.lan"),
]
MOVE_IDS = [old for old, _, _ in MOVES]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The collected list starts empty: other tests' configs add to it too."""
    monkeypatch.setattr(compat, "config_warnings", [])


def _base(tmp_path: Path) -> dict[str, Any]:
    music, torrents = tmp_path / "music", tmp_path / "torrents"
    music.mkdir(exist_ok=True)
    torrents.mkdir(exist_ok=True)
    return {
        "directory": {"download_directory": music.as_posix(), "dottorrents_dir": torrents.as_posix()},
        "tracker": {"red": {"session": "a-real-cookie"}},
    }


def _put(raw: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *tables, key = path.split(".")
    node = raw
    for table in tables:
        node = node.setdefault(table, {})
    node[key] = value
    return raw


def _read(cfg: Cfg, path: str) -> Any:
    node: Any = cfg
    for key in path.split("."):
        node = getattr(node, key)
    return node


def _load(tmp_path: Path, raw: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[Cfg, list[str]]:
    """Load ``raw`` as a config file; return it and the warning lines printed."""
    path = tmp_path / "config.toml"
    path.write_bytes(msgspec.toml.encode(raw))
    capsys.readouterr()
    cfg = _parse_config(path)
    return cfg, [line for line in capsys.readouterr().err.splitlines() if line.strip()]


def _version(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    monkeypatch.setattr(salmon.release_notification, "get_version", lambda: version)


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_an_old_name_sets_the_new_key_and_warns(tmp_path: Path, capsys, old: str, new: str, value: Any) -> None:
    cfg, warnings = _load(tmp_path, _put(_base(tmp_path), old, value), capsys)
    assert _read(cfg, new) == value
    section = new.rpartition(".")[0]
    assert warnings == [
        f"{old} is now {new} (under [{section}]): it still works in 0.21, and stops salmon from 0.22.0. "
        f"See {UPGRADE_PAGE}"
    ]


def test_the_warning_reads_as_the_brief_gives_it(tmp_path: Path, capsys) -> None:
    _, warnings = _load(tmp_path, _put(_base(tmp_path), "upload.web_interface.port", 55111), capsys)
    assert warnings == [
        "upload.web_interface.port is now upload.spectrals_viewer.port (under [upload.spectrals_viewer]): it still "
        "works in 0.21, and stops salmon from 0.22.0. "
        "See https://github.com/smokin-salmon/smoked-salmon/wiki/Upgrading-to-0.21"
    ]


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_the_old_and_the_new_name_together_are_refused(tmp_path: Path, capsys, old: str, new: str, value: Any) -> None:
    raw = _put(_put(_base(tmp_path), old, value), new, value)
    with pytest.raises(ValueError, match=f"{old} and {new} are both set"):
        _load(tmp_path, raw, capsys)


@pytest.mark.parametrize(("old", "new", "value"), MOVES, ids=MOVE_IDS)
def test_an_old_name_is_refused_from_0_22_0(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, old: str, new: str, value: Any
) -> None:
    _version(monkeypatch, "0.22.0")
    with pytest.raises(ValueError, match=f"{old} is now {new} .*stops salmon from 0.22.0"):
        _load(tmp_path, _put(_base(tmp_path), old, value), capsys)


@pytest.mark.parametrize("version", ["0.21.9", None], ids=["0.21.9", "unknown version"])
def test_an_old_name_still_works_before_0_22_0(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    _version(monkeypatch, version)
    cfg, _ = _load(tmp_path, _put(_base(tmp_path), "upload.web_interface.port", 55111), capsys)
    assert cfg.upload.spectrals_viewer.port == 55111


def test_an_old_table_emptied_by_its_moves_is_no_unknown_key(tmp_path: Path, capsys) -> None:
    raw = _base(tmp_path)
    raw["upload"] = {"web_interface": {"host": "127.0.0.2", "port": 55111, "static_root_url": "/static"}}
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert (cfg.upload.spectrals_viewer.host, cfg.upload.spectrals_viewer.port) == ("127.0.0.2", 55111)
    assert not [line for line in warnings if line.startswith("Unknown config key")]


@pytest.mark.parametrize("version", ["0.20.0", "0.22.0", "1.0.0"])
def test_static_root_url_at_its_old_default_is_warned_about_whatever_the_version(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    _version(monkeypatch, version)
    raw = _put(_base(tmp_path), "upload.web_interface.static_root_url", "/static")
    cfg, warnings = _load(tmp_path, raw, capsys)
    assert cfg.upload.spectrals_viewer.port == 55110
    assert warnings == [
        "upload.web_interface.static_root_url is no longer used, remove it: the spectrals viewer now always serves "
        f"its files at /static. See {UPGRADE_PAGE}"
    ]


def test_static_root_url_set_to_anything_else_is_refused(tmp_path: Path, capsys) -> None:
    raw = _put(_base(tmp_path), "upload.web_interface.static_root_url", "/salmon/static")
    with pytest.raises(
        ValueError, match=r"static_root_url is no longer used: .* always serves its files at /static\. Remove it"
    ):
        _load(tmp_path, raw, capsys)


def test_the_collected_warnings_hold_the_unknown_keys_too(tmp_path: Path, capsys) -> None:
    raw = _put(_put(_base(tmp_path), "upload.qwertyuiop", True), "upload.web_interface.port", 55111)
    _, warnings = _load(tmp_path, raw, capsys)
    assert len(warnings) == 2
    assert sorted(compat.config_warnings) == sorted(warnings)
    assert any(line.startswith("Unknown config key upload.qwertyuiop") for line in compat.config_warnings)


def test_a_warning_is_printed_once_per_config_path(tmp_path: Path, capsys) -> None:
    raw = _put(_base(tmp_path), "upload.web_interface.port", 55111)
    _, first = _load(tmp_path, raw, capsys)
    _, second = _load(tmp_path, raw, capsys)
    assert len(first) == 1
    assert second == []
    assert compat.config_warnings == first
