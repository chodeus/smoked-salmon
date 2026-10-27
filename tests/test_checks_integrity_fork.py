"""Backup-path reservation and restore behavior in salmon.checks.integrity."""

import anyio
import asyncclick as click
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
    """An unset MD5 shows once, as md5_unset, not again among the details."""
    _ig, result = _flac_result(monkeypatch, MD5_UNSET_OUTPUT)

    assert result.md5_unset == ("a.flac",)
    assert "MD5" not in result.details, "the raw warning must not restate what md5_unset carries"


def test_flac_progress_backspaces_do_not_eat_the_text(monkeypatch):
    """flac's \\x08 progress erasures are applied, not passed on to eat the text before them."""
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
    """flac's "ok" after the MD5 warning marks audio that decoded, whatever the exit code."""
    _ig, result = _flac_result(monkeypatch, MD5_UNSET_OUTPUT)

    assert not result.passed, "the pass/fail verdict is unchanged"
    assert result.decode_failures == (), "nothing here says the audio failed to decode"


def test_a_file_that_never_decoded_stays_a_decode_failure(monkeypatch):
    """A file flac reports an ERROR for, with no "ok", stays a decode failure beside an unset MD5."""
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
    """The result after sanitizing is the re-check's, not sanitize_integrity's return value."""
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


@pytest.fixture
def integrity_gate(monkeypatch):
    """Drive resolve_integrity_for_upload with a scripted check result and answer."""
    import importlib

    ig = importlib.import_module("salmon.checks.integrity")

    def _setup(first, *, sanitized=None, confirm=True):
        checks = iter([first, sanitized if sanitized is not None else first])
        calls: list[str] = []

        async def fake_check(_path, _i=None):
            calls.append("check")
            return next(checks)

        async def fake_sanitize(_path, _i=None):
            calls.append("sanitize")
            return True

        monkeypatch.setattr(ig, "check_integrity", fake_check)
        monkeypatch.setattr(ig, "sanitize_integrity", fake_sanitize)
        monkeypatch.setattr(click, "confirm", lambda *a, **k: calls.append("confirm") or confirm)
        return calls, ig

    return _setup


async def test_declining_to_sanitize_still_aborts_on_a_file_that_will_not_decode(integrity_gate):
    """Declining the sanitize offer is not consent to upload corrupt audio."""
    from salmon.checks.integrity import IntegrityResult, resolve_integrity_for_upload

    broken = IntegrityResult(False, "a.flac: FAILED", (), decode_failures=("a.flac",), checked=1)
    calls, _ = integrity_gate(broken, confirm=False)

    with pytest.raises(click.Abort):
        await resolve_integrity_for_upload("/music", scene=False, assume_yes=False)

    assert "sanitize" not in calls, "the user declined; nothing should have been re-encoded"


async def test_yes_all_does_not_wave_through_a_file_that_will_not_decode(integrity_gate):
    """yes_all means "take the default answer", not "ignore corruption"."""
    from salmon.checks.integrity import IntegrityResult, resolve_integrity_for_upload

    broken = IntegrityResult(False, "a.flac: FAILED", (), decode_failures=("a.flac",), checked=1)
    calls, _ = integrity_gate(broken, sanitized=broken, confirm=False)

    with pytest.raises(click.Abort):
        await resolve_integrity_for_upload("/music", scene=False, assume_yes=True)

    assert "confirm" not in calls, "yes_all answers the sanitize offer without asking"
    assert "sanitize" in calls, "yes_all takes the default answer, which is to sanitize"


async def test_unset_md5_uploads_after_declining_to_sanitize(integrity_gate):
    """A WEB album with no stored MD5 decodes fine — it must stay uploadable."""
    from salmon.checks.integrity import IntegrityResult, resolve_integrity_for_upload

    no_md5 = IntegrityResult(False, "", (), md5_unset=("a.flac",), checked=1)
    integrity_gate(no_md5, confirm=False)

    result = await resolve_integrity_for_upload("/music", scene=False, assume_yes=False)

    assert result.md5_unset == ("a.flac",), "the unsanitized result is what the caller gets back"


async def test_scene_release_aborts_before_offering_to_sanitize(integrity_gate):
    """Sanitizing a scene release de-scenes it, so the offer must never appear."""
    from salmon.checks.integrity import IntegrityResult, resolve_integrity_for_upload

    no_md5 = IntegrityResult(False, "", (), md5_unset=("a.flac",), checked=1)
    calls, _ = integrity_gate(no_md5)

    with pytest.raises(click.Abort):
        await resolve_integrity_for_upload("/music", scene=True, assume_yes=False)

    assert calls == ["check"], "no confirm, no sanitize"


@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_the_check_command_sanitizes_only_when_told_to(monkeypatch, tmp_path, kind):
    """Enter at the sanitize question keeps the files as they are, as upstream's: sanitizing re-encodes them."""
    import importlib

    ig = importlib.import_module("salmon.checks.integrity")
    (tmp_path / "a.flac").write_bytes(b"fLaC")
    sanitized: list[str] = []

    async def failing(_path):
        return ig.IntegrityResult(False, "", (), md5_unset=("a.flac",), checked=1)

    async def sanitize(path):
        sanitized.append(path)

    monkeypatch.setattr(ig, "rewrite_refusal", lambda _path: None)
    monkeypatch.setattr(ig, "check_integrity", failing)
    monkeypatch.setattr(ig, "sanitize_and_verify", sanitize)
    monkeypatch.setattr(click, "confirm", lambda *_args, default=False, **_kwargs: default)

    await ig.handle_integrity_check(str(tmp_path / "a.flac") if kind == "file" else str(tmp_path))

    assert sanitized == []


@pytest.mark.parametrize(("check", "tool"), [("_check_flac_integrity", "flac"), ("_check_mp3_integrity", "mp3val")])
def test_a_tool_that_cannot_run_is_named_and_still_fails_closed(monkeypatch, check, tool):
    import importlib

    integrity = importlib.import_module("salmon.checks.integrity")

    async def missing(*_args, **_kwargs):
        raise FileNotFoundError(f"No such file or directory: '{tool}'")

    monkeypatch.setattr(integrity.anyio, "run_process", missing)
    result = anyio.run(getattr(integrity, check), "/album/01.x")

    assert result.passed is False
    assert result.decode_failures == ("01.x",)
    assert f"01.x: could not run {tool} (No such file or directory: '{tool}')" in click.unstyle(result.details)
