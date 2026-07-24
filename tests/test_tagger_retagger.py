from types import SimpleNamespace

from salmon import cfg
from salmon.tagger.retagger import rename_files


def test_rename_files_can_flatten_multi_disc_tracks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("a")
    (tmp_path / "02.flac").write_text("b")

    tags = {
        "01.flac": SimpleNamespace(tracknumber="01", discnumber="1"),
        "02.flac": SimpleNamespace(tracknumber="01", discnumber="2"),
    }
    metadata = {
        "tracks": {
            "1": {"1": {"artists": [("Artist", "main")], "title": "Track 1"}},
            "2": {"1": {"artists": [("Artist", "main")], "title": "Track 2"}},
        }
    }

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "1.1.flac").exists()
    assert (tmp_path / "2.1.flac").exists()
    assert not (tmp_path / "CD01").exists()
    assert not (tmp_path / "CD02").exists()