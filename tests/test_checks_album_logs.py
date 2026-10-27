"""The pre-flight reads each rip log as salmon up checks it: score, every appended log's checksum, and its CRCs."""

from pathlib import Path
from types import SimpleNamespace

import anyio
import cambia
import pytest

from salmon.checks import album_web
from salmon.errors import CRCMismatchError, LogCheckSkipped


def _output(*integrities: cambia.Integrity, score: str = "100") -> SimpleNamespace:
    """What parse_log gives for a log of one or more appended rips."""
    rips = [SimpleNamespace(checksum=SimpleNamespace(integrity=integrity)) for integrity in integrities]
    return SimpleNamespace(
        evaluation_combined=[SimpleNamespace(combined_score=score)], parsed=SimpleNamespace(parsed_logs=rips)
    )


def _read(monkeypatch, tmp_path: Path, output: SimpleNamespace, crcs: Exception | None = None) -> list[dict]:
    """run_log_check on an album holding one rip.log, with cambia's reading and the CRC check replaced."""
    album_path = tmp_path / "Album"
    album_path.mkdir()
    (album_path / "rip.log").write_text("test log", encoding="utf-8")
    verified: list[str] = []

    async def verify(_output, logpath: str, _basepath: str) -> None:
        verified.append(logpath)
        if crcs is not None:
            raise crcs

    monkeypatch.setattr(album_web, "parse_log", lambda _logpath: output)
    monkeypatch.setattr(album_web, "verify_log_crcs", verify)
    result = anyio.run(album_web.run_log_check, str(album_path))
    return [*result["logs"], {"verified": len(verified)}]


def test_an_edited_rerip_appended_to_a_good_log_is_read_as_edited(monkeypatch, tmp_path) -> None:
    logs = _read(monkeypatch, tmp_path, _output(cambia.Integrity.Match, cambia.Integrity.Mismatch))

    assert logs == [
        {
            "file": "rip.log",
            "score": 100,
            "checksum_integrity": "Mismatch",
            "crcs": "skipped",
            "crcs_detail": "the log was edited",
        },
        {"verified": 0},
    ]


@pytest.mark.parametrize(
    ("raised", "crcs", "detail"),
    [
        (None, "match", ""),
        (CRCMismatchError("CRC Mismatch"), "mismatch", ""),
        (LogCheckSkipped("No audio files found!"), "skipped", "No audio files found!"),
        (RuntimeError("Error decoding audio"), "error", "Error decoding audio"),
    ],
    ids=["match", "mismatch", "skipped", "unverifiable"],
)
def test_each_crc_outcome_is_kept_apart(
    monkeypatch, tmp_path, raised: Exception | None, crcs: str, detail: str
) -> None:
    logs = _read(monkeypatch, tmp_path, _output(cambia.Integrity.Match), raised)

    assert logs[0]["crcs"] == crcs
    assert logs[0]["crcs_detail"] == detail
    assert logs[1] == {"verified": 1}


def test_a_log_without_a_score_is_still_checked(monkeypatch, tmp_path) -> None:
    logs = _read(monkeypatch, tmp_path, _output(cambia.Integrity.Unknown, score=""))

    assert (logs[0]["score"], logs[0]["checksum_integrity"], logs[0]["crcs"]) == (None, "Unknown", "match")


def test_a_skip_names_the_album_folder_not_its_parents(monkeypatch, tmp_path) -> None:
    under = tmp_path / "Album" / "CD2"
    logs = _read(monkeypatch, tmp_path, _output(cambia.Integrity.Match), LogCheckSkipped(f"only 1 file under {under}"))

    assert logs[0]["crcs_detail"] == f"only 1 file under {Path('Album', 'CD2')}"


def test_a_log_cambia_cannot_parse_is_an_error_named_from_the_album(monkeypatch, tmp_path) -> None:
    album_path = tmp_path / "Album"
    album_path.mkdir()
    logpath = album_path / "rip.log"
    logpath.write_text("not a log", encoding="utf-8")

    def unparseable(path: str) -> None:
        raise LogCheckSkipped(f"Could not parse {path}: no rip found")

    monkeypatch.setattr(album_web, "parse_log", unparseable)

    result = anyio.run(album_web.run_log_check, str(album_path))

    assert result == {"logs": [{"file": "rip.log", "error": "Could not parse rip.log: no rip found"}]}
