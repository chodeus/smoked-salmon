"""Startup tmp cleanup: stale scratch goes, anything a running salmon may hold stays."""

import os
import time

from salmon import cfg
from salmon.run import TMP_MAX_AGE_HOURS, cleanup_tmp_dir


def _age(path, hours):
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg.directory, "tmp_dir", str(tmp_path))
    monkeypatch.setattr(cfg.directory, "clean_tmp_dir", True)
    return tmp_path


def test_stale_scratch_is_removed(monkeypatch, tmp_path):
    tmp = _tmp(monkeypatch, tmp_path)
    old_dir = tmp / "spectrals_old"
    old_dir.mkdir()
    (old_dir / "01 Full.png").write_bytes(b"PNG")
    old_file = tmp / "leftover.txt"
    old_file.write_text("x")
    for path in (old_dir / "01 Full.png", old_dir, old_file):
        _age(path, TMP_MAX_AGE_HOURS + 1)

    cleanup_tmp_dir()

    assert not old_dir.exists()
    assert not old_file.exists()


def test_a_recent_folder_is_left_alone(monkeypatch, tmp_path):
    # The case that matters: a CLI command must not wipe a running webui's spectrals.
    tmp = _tmp(monkeypatch, tmp_path)
    live = tmp / "spectrals_live"
    live.mkdir()
    (live / "01 Full.png").write_bytes(b"PNG")

    cleanup_tmp_dir()

    assert live.exists()


def test_a_folder_written_into_right_now_survives_its_own_old_timestamp(monkeypatch, tmp_path):
    # Directory mtime does not move when a file inside it is rewritten, so the
    # children are what say the folder is still in use.
    tmp = _tmp(monkeypatch, tmp_path)
    busy = tmp / "spectrals_busy"
    busy.mkdir()
    fresh = busy / "01 Full.png"
    fresh.write_bytes(b"PNG")
    _age(busy, TMP_MAX_AGE_HOURS + 1)

    cleanup_tmp_dir()

    assert busy.exists()
    assert fresh.exists()


def test_nothing_is_touched_when_the_option_is_off(monkeypatch, tmp_path):
    tmp = _tmp(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg.directory, "clean_tmp_dir", False)
    stale = tmp / "spectrals_old"
    stale.mkdir()
    _age(stale, TMP_MAX_AGE_HOURS + 10)

    cleanup_tmp_dir()

    assert stale.exists()


def test_an_unreadable_entry_does_not_stop_the_sweep(monkeypatch, tmp_path):
    tmp = _tmp(monkeypatch, tmp_path)
    dangling = tmp / "dangling"
    dangling.symlink_to(tmp / "does-not-exist")
    stale = tmp / "spectrals_old"
    stale.mkdir()
    _age(stale, TMP_MAX_AGE_HOURS + 1)

    cleanup_tmp_dir()

    assert not stale.exists(), "one bad entry must not abandon the rest of the sweep"
