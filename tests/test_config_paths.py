"""Where salmon looks for config.toml."""

from pathlib import Path

from salmon.config import CONFIG_DIR_ENV, get_user_cfg_path


def test_config_dir_env_overrides_platform_dir(monkeypatch) -> None:
    # Containers mount a single /config; the file must sit directly in it.
    monkeypatch.setenv(CONFIG_DIR_ENV, "/config")
    assert get_user_cfg_path() == Path("/config/config.toml")


def test_config_dir_env_expands_user(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, "~/salmon-cfg")
    assert get_user_cfg_path() == Path.home() / "salmon-cfg" / "config.toml"


def test_empty_config_dir_env_falls_back_to_platform_dir(monkeypatch) -> None:
    monkeypatch.setenv(CONFIG_DIR_ENV, "")
    path = get_user_cfg_path()
    assert path.name == "config.toml"
    assert "smoked-salmon" in str(path)


def test_unset_config_dir_env_uses_platform_dir(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    path = get_user_cfg_path()
    assert path.name == "config.toml"
    assert "smoked-salmon" in str(path)
