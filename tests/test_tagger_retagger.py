from types import SimpleNamespace

from salmon import cfg
from salmon.tagger.retagger import _remap_spectral_ids, rename_files


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

    assert (tmp_path / "1.01.flac").exists()
    assert (tmp_path / "2.01.flac").exists()
    assert not (tmp_path / "CD01").exists()
    assert not (tmp_path / "CD02").exists()


def test_rename_files_swap_preserves_content(tmp_path, monkeypatch) -> None:
    # Files are mis-numbered so retag swaps their names (01<->02). A single-phase
    # os.rename would clobber a source before its content moved; two-phase must
    # preserve every file's bytes across the swap.
    monkeypatch.setattr(cfg.upload.formatting, "file_template", "{tracknumber}")
    monkeypatch.setattr(cfg.upload.formatting, "split_multi_disc_into_folders", False)

    (tmp_path / "01.flac").write_text("A")  # tagged track 2 -> target 02.flac
    (tmp_path / "02.flac").write_text("B")  # tagged track 1 -> target 01.flac

    tags = {
        "01.flac": SimpleNamespace(tracknumber="02", discnumber="1"),
        "02.flac": SimpleNamespace(tracknumber="01", discnumber="1"),
    }
    metadata = {
        "tracks": {
            "1": {
                "1": {"artists": [("Artist", "main")], "title": "Track 1"},
                "2": {"artists": [("Artist", "main")], "title": "Track 2"},
            }
        }
    }

    rename_files(str(tmp_path), tags, metadata, auto_rename=True, spectral_ids=None, source="CD")

    assert (tmp_path / "01.flac").read_text() == "B"
    assert (tmp_path / "02.flac").read_text() == "A"
    assert not list(tmp_path.glob(".salmon-rename-*"))  # no temp files left behind


def test_remap_spectral_ids_does_not_chain():
    # to_rename forms a chain a->b, b->c. Track a's spectral must land on b (its
    # direct target), not be dragged through to c by sequential application.
    spectral_ids = {1: "a.flac", 2: "b.flac"}
    _remap_spectral_ids(spectral_ids, [("a.flac", "b.flac"), ("b.flac", "c.flac")])
    assert spectral_ids == {1: "b.flac", 2: "c.flac"}