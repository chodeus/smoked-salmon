"""Backup-path reservation and restore behavior in salmon.checks.integrity."""

import anyio
import pytest

from salmon.checks.integrity import _reserve_backup_path, _sanitize_flac


def test_reserve_backup_path_claims_atomically_and_skips_stale(tmp_path) -> None:
    f = tmp_path / "a.flac"
    f.write_text("x")

    first = _reserve_backup_path(str(f))
    assert first == f"{f}.corrupted"
    # The claim creates a placeholder, so a second claimer can never pick the
    # same name (O_EXCL) — this is the concurrency guarantee.
    assert (tmp_path / "a.flac.corrupted").exists()

    second = _reserve_backup_path(str(f))
    assert second == f"{f}.corrupted.1"
    assert (tmp_path / "a.flac.corrupted.1").exists()

    third = _reserve_backup_path(str(f))
    assert third == f"{f}.corrupted.2"


async def test_sanitize_flac_restores_original_on_cancellation(tmp_path, monkeypatch) -> None:
    # Cancellation is not an Exception; without the BaseException handler the
    # file would stay renamed aside as .corrupted.
    f = tmp_path / "a.flac"
    f.write_text("DATA")

    async def cancelled(*_args, **_kwargs):
        raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr(anyio, "run_process", cancelled)

    with pytest.raises(anyio.get_cancelled_exc_class()):
        await _sanitize_flac(str(f))

    assert f.read_text() == "DATA"
    assert not list(tmp_path.glob("*.corrupted*"))


def test_a_warning_from_mp3val_is_carried_as_a_concern_not_swallowed(monkeypatch):
    """mp3val exits 0 while describing the damage, so its text is the verdict."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(
            returncode=0,
            stdout=b'INFO: "a.mp3": 100 MPEG frames\nWARNING: "a.mp3": It seems that file is truncated\n',
        )

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert result.passed, "a truncated-but-playable file still decodes; it must not block"
    assert len(result.concerns) == 1
    assert "truncated" in result.concerns[0]


def test_an_mp3val_error_fails_the_check(monkeypatch):
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=0, stdout=b'ERROR: "a.mp3": Unable to open file\n')

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert not result.passed
    assert "Unable to open file" in result.details, "a failure must carry its reason to the caller"


def test_a_clean_mp3_passes_with_nothing_to_report(monkeypatch):
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=0, stdout=b'INFO: "a.mp3": 7320 MPEG frames (MPEG 1 Layer III), CBR\n')

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert result.passed
    assert result.concerns == ()


def test_a_file_mp3val_could_not_open_is_not_a_pass(monkeypatch):
    """mp3val exits 0 and prints no ERROR: prefix for an unreadable or empty
    file, so a run that produced no analysis must not read as clean."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=0, stdout=b'Cannot open input file "a.mp3" or it is empty\nDone!\n')

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert not result.passed
    assert "Cannot open input file" in result.details, "the reason must reach the details, not vanish"


def test_a_warning_is_not_repeated_in_the_details(monkeypatch):
    """A line in both details and concerns is rendered to the reader twice."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(
            returncode=0,
            stdout=b'INFO: "a.mp3": 100 MPEG frames\nWARNING: "a.mp3": It seems that file is truncated\n',
        )

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert "truncated" in result.concerns[0]
    assert "truncated" not in result.details
    assert "100 MPEG frames" in result.details


def test_an_error_alongside_an_analysis_still_reaches_the_details(monkeypatch):
    """The empty-details fallback hides this: with an INFO line present the
    fallback never fires, so an ERROR must be carried explicitly."""
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(
            returncode=0,
            stdout=b'INFO: "a.mp3": 100 MPEG frames\nERROR: "a.mp3": Unknown file format\n',
        )

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    result = asyncio.run(ig._check_mp3_integrity("/music/a.mp3"))

    assert not result.passed
    assert "Unknown file format" in result.details


def _flac_result(monkeypatch, stdout: bytes, returncode: int = 1):
    import asyncio
    import importlib
    from types import SimpleNamespace

    ig = importlib.import_module("salmon.checks.integrity")

    async def fake_run(_cmd, check=False):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(ig.anyio, "run_process", fake_run)
    return ig, asyncio.run(ig._check_flac_integrity("/music/a.flac"))


# Verified against flac 1.5.0: an unset MD5 prints the warning, then a bare "ok".
MD5_UNSET_OUTPUT = (
    b"a.flac: WARNING, cannot check MD5 signature since it was unset in the STREAMINFO\nok                    \n"
)


def test_an_unset_md5_is_reported_once_not_twice(monkeypatch):
    """The warning line and the summary are the same fact; 17 tracks became 34 lines."""
    _ig, result = _flac_result(monkeypatch, MD5_UNSET_OUTPUT)

    assert result.md5_unset == ("a.flac",)
    assert "MD5" not in result.details, "the raw warning must not restate what md5_unset carries"


def test_flac_progress_backspaces_do_not_eat_the_text(monkeypatch):
    """flac erases "testing, N% complete" with \\x08; passed through raw they eat
    the characters in front of them wherever the details are rendered."""
    ig, result = _flac_result(
        monkeypatch,
        b"a.flac: testing, 72% complete" + b"\x08" * 21 + b"WARNING, cannot check MD5 signature "
        b"since it was unset in the STREAMINFO\nok                    \n",
    )

    assert result.md5_unset == ("a.flac",)
    assert "\x08" not in result.details
    assert "% complete" not in result.details
    assert ig._resolve_overstrikes("abc\x08\x08d") == "ad"


def test_an_unset_md5_is_not_counted_as_a_decode_failure(monkeypatch):
    """-w makes the warning exit non-zero, so the return code cannot say whether the
    audio decoded. flac saying "ok" can."""
    _ig, result = _flac_result(monkeypatch, MD5_UNSET_OUTPUT)

    assert not result.passed, "the pass/fail verdict is unchanged"
    assert result.decode_failures == (), "nothing here says the audio failed to decode"


def test_a_file_that_never_decoded_stays_a_decode_failure(monkeypatch):
    """A truncated file prints ERROR and no "ok" — it must not ride the MD5 downgrade."""
    _ig, result = _flac_result(
        monkeypatch,
        b"a.flac: *** Got error code 4:FLAC__STREAM_DECODER_ERROR_STATUS_BAD_METADATA\n"
        b"a.flac: ERROR while decoding metadata\n",
    )

    assert not result.passed
    assert result.decode_failures == ("a.flac",)
    assert result.md5_unset == ()


def test_a_silent_decode_failure_is_not_announced_as_a_clean_missing_md5():
    """A failure that parsed to no detail text must not borrow the MD5 headline."""
    import importlib

    ig = importlib.import_module("salmon.checks.integrity")
    rendered = ig.format_integrity(
        ig.IntegrityResult(False, "", (), md5_unset=("a.flac",), decode_failures=("b.flac",), checked=2)
    )

    assert "Failed integrity check" in rendered
    assert "no MD5 signature stored" not in rendered.splitlines()[0]


async def test_sanitize_and_verify_rechecks_instead_of_trusting_the_return(monkeypatch):
    """sanitize_integrity returning True is not evidence the MD5 got set.

    Every caller acts on "it is fixed now" — the upload even says so in the release
    description — so the claim is re-checked, not inferred.
    """
    import importlib

    ig = importlib.import_module("salmon.checks.integrity")

    calls: list[str] = []

    async def fake_sanitize(_path, _i=None):
        calls.append("sanitize")
        return True

    async def fake_check(_path, _i=None):
        calls.append("check")
        return ig.IntegrityResult(False, "", (), md5_unset=("a.flac",), checked=1)

    monkeypatch.setattr(ig, "sanitize_integrity", fake_sanitize)
    monkeypatch.setattr(ig, "check_integrity", fake_check)

    result = await ig.sanitize_and_verify("/music")

    assert calls == ["sanitize", "check"], "the re-check must run after sanitizing"
    assert not result.passed, "the re-check's verdict is returned, not sanitize's return value"
