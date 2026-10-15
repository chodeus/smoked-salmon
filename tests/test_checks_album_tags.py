"""The pre-flight's MP3 tags check: a filled-in ID3v1 tag beside a blank ID3v2 one, which RED and OPS can trump."""

import anyio
import pytest
from test_tag_rules_upstream import (  # pyright: ignore[reportMissingImports]
    _write_mp3_v1_and_blank_v2,
    _write_mp3_v2_only,
)

from salmon.checks import album
from salmon.checks import preflight as pf


def test_a_dual_id3_mp3_is_named_and_a_good_one_is_not(tmp_path) -> None:
    _write_mp3_v1_and_blank_v2(tmp_path / "01.mp3")
    _write_mp3_v2_only(tmp_path / "02.mp3")
    (tmp_path / "03.flac").write_bytes(b"not read")

    result = anyio.run(album.run_tag_check, str(tmp_path))

    assert result == {"mp3s": 2, "dual_id3": ["01.mp3"], "errors": []}


def test_an_unreadable_mp3_is_an_error_not_a_pass(tmp_path, monkeypatch) -> None:
    (tmp_path / "01.mp3").write_bytes(b"not an mp3")

    def unreadable(_path: str) -> bool:
        raise OSError("test: unreadable")

    monkeypatch.setattr(album, "has_blank_id3v2_alongside_id3v1", unreadable)

    result = anyio.run(album.run_tag_check, str(tmp_path))

    assert result == {"mp3s": 1, "dual_id3": [], "errors": [{"file": "01.mp3", "error": "test: unreadable"}]}


@pytest.mark.parametrize(
    ("result", "verdict", "said"),
    [
        ({"mp3s": 2, "dual_id3": ["01.mp3"], "errors": []}, pf.WARN, "1 MP3(s) with a filled-in ID3v1 tag"),
        ({"mp3s": 1, "dual_id3": [], "errors": [{"file": "01.mp3", "error": "e"}]}, pf.WARN, "Could not read"),
        ({"mp3s": 0, "dual_id3": [], "errors": []}, pf.SKIP, "No MP3 file."),
        ({"mp3s": 3, "dual_id3": [], "errors": []}, pf.OK, "in 3 MP3(s)."),
    ],
    ids=["dual", "unreadable", "no-mp3", "clean"],
)
def test_the_tags_verdict(result: dict, verdict: str, said: str) -> None:
    found, detail = pf._tags_verdict(result, {})

    assert found == verdict
    assert said in detail
