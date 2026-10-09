"""What this fork's cross-upload adds to upstream's: a folder of .torrent files as INPUT, conversions only into a
group TARGET has, --all-formats, and the 16-bit rule read from the files whatever the label.

The runs use test_cross_upload.py's fake trackers, image hosts and seedbox.
"""

from pathlib import Path
from typing import Any

import anyio
import asyncclick as click
import pytest
from asyncclick.testing import CliRunner
from test_checks_do_not_upload import write_lists  # pyright: ignore[reportMissingImports]
from test_cross_upload import (  # pyright: ignore[reportMissingImports]
    FOLDER,
    TARGET_GROUP_ID,
    TORRENT_ID,
    FakeTracker,
    Run,
    _album,
    _cross_upload,
    _red_answer,
    _source_has,
    _transcode,
    _within_the_plans_bound,
    dirs,  # noqa: F401
    settings,  # noqa: F401
)

import salmon.cross_upload as cross_upload_module
import salmon.trackers
from salmon.cross_upload import _folder_torrents

TARGET_FLAC_ID = 700500


def _torrent_files(folder: Path, *names: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / name for name in names]
    for path in paths:
        path.write_bytes(b"d4:infod4:name1:xee")
    return paths


# A folder as INPUT


def test_a_folder_gives_each_torrent_file_directly_in_it(tmp_path: Path) -> None:
    folder = tmp_path / "torrents"
    upper, lower = _torrent_files(folder, "A.TORRENT", "b.torrent")
    _torrent_files(folder / "nested", "c.torrent")
    (folder / "d.torrent").mkdir()
    (folder / "notes.txt").write_text("not a torrent")
    outside = _torrent_files(tmp_path / "elsewhere", "e.torrent")[0]
    (folder / "e.torrent").symlink_to(outside)

    assert _folder_torrents(str(folder)) == [str(upper), str(lower)]


def test_a_folder_with_no_torrent_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a torrent")
    with pytest.raises(click.UsageError, match="No .torrent files found"):
        _folder_torrents(str(tmp_path))


def test_a_torrent_id_is_its_own_input_even_beside_a_folder_of_that_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _torrent_files(tmp_path / "42", "a.torrent")
    assert _folder_torrents("42") == ["42"]


def _refused_before_any_request(monkeypatch, args: list[str]) -> str:
    monkeypatch.setattr(salmon.trackers, "get_class", lambda _code: pytest.fail("no tracker client may be made"))
    result = anyio.run(lambda: CliRunner().invoke(cross_upload_module.cross_upload, [*args, "OPS", "RED"]))
    assert result.exit_code == 2, result.output
    return result.output


def test_a_folder_of_more_releases_than_a_run_takes_is_refused_before_any_request(monkeypatch, tmp_path) -> None:
    _torrent_files(tmp_path, *(f"{number}.torrent" for number in range(6)))
    assert "At most 5 releases per run, not 6." in _refused_before_any_request(monkeypatch, [str(tmp_path)])


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (["1", "--target-group-id", "9"], "--target-group-id needs --transcode, --downconvert or --all."),
        (["1", "--target-group-id", "9", "-g", "3", "--all"], "--group-id and --target-group-id cannot go together."),
        (["1", "2", "--target-group-id", "9", "--all"], "--path, --group-id and --target-group-id go with a single"),
    ],
    ids=["no-conversion", "with-group-id", "two-releases"],
)
def test_target_group_id_is_checked_before_any_request(monkeypatch, args: list[str], said: str) -> None:
    assert said in _refused_before_any_request(monkeypatch, args)


def test_a_folder_of_two_torrents_is_two_releases_for_target_group_id(monkeypatch, tmp_path) -> None:
    _torrent_files(tmp_path, "a.torrent", "b.torrent")
    output = _refused_before_any_request(monkeypatch, [str(tmp_path), "--target-group-id", "9", "--all"])
    assert "go with a single release" in output


def test_all_formats_is_another_name_for_all(monkeypatch, dirs) -> None:  # noqa: F811
    album = _album(dirs.downloads / FOLDER)
    run = _cross_upload(
        monkeypatch,
        dirs,
        [str(TORRENT_ID), "--all-formats", "--dry-run", "-yyy"],
        input="\n",
        prepare=_source_has(album),
        transcode=_transcode,
    )

    assert run.result.exit_code == 0, run.output
    assert "   uploads: FLAC Lossless, MP3 320, MP3 V0" in run.output


# Conversions only, into a group TARGET has


def _target_group(*torrents: dict[str, Any]) -> dict[str, Any]:
    return {"group": {"id": TARGET_GROUP_ID, "year": 2018}, "torrents": list(torrents)}


def _web_flac(**fields: Any) -> dict[str, Any]:
    """TARGET's WEB FLAC of the fixture's edition."""
    return {
        "id": TARGET_FLAC_ID,
        "media": "WEB",
        "format": "FLAC",
        "encoding": "Lossless",
        "remastered": True,
        "remasterYear": 2023,
        "remasterTitle": "",
        **fields,
    }


def _conversions_only(monkeypatch, dirs, group: dict[str, Any], *extra: str, album: Path | None = None) -> Run:  # noqa: F811
    album = album or _album(dirs.downloads / FOLDER)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album)(source, target)
        target.groups[TARGET_GROUP_ID] = group

    args = [str(TORRENT_ID), "-yyy", "--target-group-id", str(TARGET_GROUP_ID), "--transcode", "V0", *extra]
    return _cross_upload(monkeypatch, dirs, args, input="\n", prepare=prepare, transcode=_transcode)


def test_conversions_only_go_into_the_group_named_as_made_from_its_flac(monkeypatch, dirs) -> None:  # noqa: F811
    run = _conversions_only(monkeypatch, dirs, _target_group(_web_flac()))

    assert run.result.exit_code == 0, run.output
    assert f"   uploads: MP3 V0 (only these, into {run.target.url}/torrents.php?id={TARGET_GROUP_ID})" in run.output
    (post,) = run.target.posts()
    assert (post.fields["format"][0], post.fields["bitrate"][0]) == ("MP3", "V0 (VBR)")
    assert post.fields["groupid"][0] == str(TARGET_GROUP_ID)
    flac_url = f"{run.target.url}/torrents.php?torrentid={TARGET_FLAC_ID}"
    assert post.fields["release_desc"][0].startswith(f"[b]Source:[/b] {flac_url}\n")
    # No search for the release: the group is named.
    assert "GET ajax.php?action=browse" not in run.target.steps()
    assert _within_the_plans_bound(run)


@pytest.mark.parametrize(
    "group",
    [
        _target_group(),
        _target_group(_web_flac(encoding="24bit Lossless")),
        _target_group(_web_flac(remasterYear=2019)),
    ],
    ids=["empty", "other-encoding", "other-edition"],
)
def test_conversions_only_into_a_group_without_the_editions_flac_upload_nothing(monkeypatch, dirs, group) -> None:  # noqa: F811
    run = _conversions_only(monkeypatch, dirs, group)

    assert run.result.exit_code == 1
    assert f"group {TARGET_GROUP_ID} on RED has no FLAC of this edition to convert from" in run.output
    assert run.target.posts() == []


def test_conversions_only_leave_out_a_format_the_group_already_has(monkeypatch, dirs) -> None:  # noqa: F811
    held = {**_web_flac(id=TARGET_FLAC_ID + 1), "format": "MP3", "encoding": "V0 (VBR)"}
    run = _conversions_only(monkeypatch, dirs, _target_group(_web_flac(), held))

    assert run.result.exit_code == 0, run.output
    assert "Not uploading MP3 V0: the group already has it in this edition." in run.output
    assert run.target.posts() == []


def test_a_release_on_the_targets_list_gets_no_conversions_either(monkeypatch, dirs, tmp_path) -> None:  # noqa: F811
    write_lists(monkeypatch, tmp_path / "lists", RED="[[entry]]\nartist = 'sample3000'\nnote = 'Fakes only.'\n")
    run = _conversions_only(monkeypatch, dirs, _target_group(_web_flac()))

    assert run.result.exit_code == 1
    assert "Fakes only." in run.output
    assert run.target.sent == []


# The 16-bit rule, from the files


def test_a_16bit_flac_above_48khz_labelled_24bit_is_still_refused_for_ops(monkeypatch, dirs) -> None:  # noqa: F811
    album = _album(dirs.downloads / FOLDER, 96000)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=_red_answer(source.url))(source, target)
        source.torrents[TORRENT_ID]["torrent"]["encoding"] = "24bit Lossless"

    run = _cross_upload(
        monkeypatch, dirs, [str(TORRENT_ID), "-yyy"], input="\n", source="RED", target="OPS", prepare=prepare
    )

    assert run.result.exit_code == 1
    assert "2 16bit file(s) above 48 kHz" in run.output
    assert "OPS refuses them." in run.output
    assert run.target.sent == []


def test_conversions_of_a_16bit_flac_above_48khz_go_into_an_ops_group(monkeypatch, dirs) -> None:  # noqa: F811
    album = _album(dirs.downloads / FOLDER, 96000)

    def prepare(source: FakeTracker, target: FakeTracker) -> None:
        _source_has(album, answer=_red_answer(source.url))(source, target)
        response = source.torrents[TORRENT_ID]
        target.groups[TARGET_GROUP_ID] = _target_group(
            _web_flac(
                encoding=response["torrent"]["encoding"],
                remasterYear=response["torrent"]["remasterYear"],
                remasterTitle=response["torrent"]["remasterTitle"],
            )
        )

    args = [str(TORRENT_ID), "-yyy", "--target-group-id", str(TARGET_GROUP_ID), "--transcode", "V0"]
    run = _cross_upload(
        monkeypatch, dirs, args, input="\n", source="RED", target="OPS", prepare=prepare, transcode=_transcode
    )

    assert run.result.exit_code == 0, run.output
    # The rule is about the FLAC, which OPS has already: an MP3 made from it is not refused.
    assert "16bit file(s) above 48 kHz" not in run.output
    assert len(run.target.posts()) == 1
