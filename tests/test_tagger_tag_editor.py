"""prompt_editor must never claim an editor opened when none did.

puddletag needs a desktop session, so over the web interface and in a container
it silently fails; the tags were then re-read unchanged and the user was none
the wiser.
"""

import json

import pytest

from salmon.tagger import tags as tags_mod


@pytest.fixture
def album(album_dir, monkeypatch):
    """Two tracks with readable tags, and no puddletag on PATH."""
    fake = {
        "01.flac": {"title": "One", "artist": "X", "album": "Y"},
        "02.flac": {"title": "Two", "artist": "X", "album": "Y"},
    }
    saved: list[str] = []

    class _Tag:
        def __init__(self, name):
            self.name = name

        def __getattr__(self, item):
            return fake[self.name].get(item)

        def __setattr__(self, key, value):
            if key == "name":
                super().__setattr__(key, value)
            else:
                fake[self.name][key] = value

        def save(self):
            saved.append(self.name)

    monkeypatch.setattr(tags_mod, "gather_tags", lambda _p: {n: _Tag(n) for n in fake})
    monkeypatch.setattr(tags_mod.shutil, "which", lambda _n: None)
    return {"path": str(album_dir), "fake": fake, "saved": saved}


def test_discarding_the_editor_writes_nothing(album, monkeypatch):
    monkeypatch.setattr(tags_mod.click, "edit", lambda *_a, **_kw: None)
    assert tags_mod.edit_tags_as_json(album["path"]) is False
    assert album["saved"] == []


def test_invalid_json_writes_nothing(album, monkeypatch):
    monkeypatch.setattr(tags_mod.click, "edit", lambda *_a, **_kw: "{not json")
    assert tags_mod.edit_tags_as_json(album["path"]) is False
    assert album["saved"] == []


def test_an_unchanged_document_writes_nothing(album, monkeypatch):
    monkeypatch.setattr(tags_mod.click, "edit", lambda text, **_kw: text)
    assert tags_mod.edit_tags_as_json(album["path"]) is False
    assert album["saved"] == []


def test_only_the_edited_file_is_written(album, monkeypatch):
    def edit(text, **_kw):
        doc = json.loads(text)
        doc["01.flac"]["title"] = "Renamed"
        return json.dumps(doc)

    monkeypatch.setattr(tags_mod.click, "edit", edit)
    assert tags_mod.edit_tags_as_json(album["path"]) is True
    assert album["saved"] == ["01.flac"]
    assert album["fake"]["01.flac"]["title"] == "Renamed"
    assert album["fake"]["02.flac"]["title"] == "Two"


def test_an_unknown_filename_aborts_without_writing(album, monkeypatch):
    def edit(text, **_kw):
        doc = json.loads(text)
        doc["99. injected.flac"] = {"title": "nope"}
        return json.dumps(doc)

    monkeypatch.setattr(tags_mod.click, "edit", edit)
    assert tags_mod.edit_tags_as_json(album["path"]) is False
    assert album["saved"] == []


async def test_without_puddletag_the_json_editor_is_used(album, monkeypatch):
    used: list[str] = []
    monkeypatch.setattr(tags_mod, "edit_tags_as_json", lambda _p: used.append("json") or True)
    assert await tags_mod.open_tag_editor(album["path"]) is True
    assert used == ["json"], "a missing puddletag must not be reported as a successful edit"


async def test_puddletag_is_preferred_when_installed(album, monkeypatch):
    launched: list[list[str]] = []

    class _Result:
        returncode = 0

    async def fake_run(args, **_kw):
        launched.append(args)
        return _Result()

    monkeypatch.setattr(tags_mod.shutil, "which", lambda _n: "/usr/bin/puddletag")
    monkeypatch.setattr(tags_mod.anyio, "run_process", fake_run)
    monkeypatch.setattr(tags_mod, "edit_tags_as_json", lambda _p: pytest.fail("should not fall back"))
    assert await tags_mod.open_tag_editor(album["path"]) is True
    assert launched == [["puddletag", album["path"]]]


async def test_a_failed_puddletag_launch_falls_back(album, monkeypatch):
    class _Result:
        returncode = 1

    async def fake_run(_args, **_kw):
        return _Result()

    monkeypatch.setattr(tags_mod.shutil, "which", lambda _n: "/usr/bin/puddletag")
    monkeypatch.setattr(tags_mod.anyio, "run_process", fake_run)
    monkeypatch.setattr(tags_mod, "edit_tags_as_json", lambda _p: True)
    assert await tags_mod.open_tag_editor(album["path"]) is True


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda d: d.__setitem__("02.flac", None), "a null file entry"),
        (lambda d: d.__setitem__("02.flac", "not an object"), "a string file entry"),
        (lambda d: d["02.flac"].__setitem__("title", {"nested": 1}), "a nested object value"),
        (lambda d: d["02.flac"].__setitem__("artist", [1, 2]), "a list of non-strings"),
    ],
)
def test_a_document_broken_partway_down_writes_nothing(album, monkeypatch, mutate, why):
    """The first file is valid and changed; the second is not. Neither may be written."""

    def edit(text, **_kw):
        doc = json.loads(text)
        doc["01.flac"]["title"] = "Renamed"  # a real change, earlier in the document
        mutate(doc)
        return json.dumps(doc)

    monkeypatch.setattr(tags_mod.click, "edit", edit)
    assert tags_mod.edit_tags_as_json(album["path"]) is False, why
    assert album["saved"] == [], f"{why} left a partial write"
    assert album["fake"]["01.flac"]["title"] == "One", f"{why} mutated the first file"


def test_a_non_object_root_is_rejected(album, monkeypatch):
    monkeypatch.setattr(tags_mod.click, "edit", lambda *_a, **_kw: '["not", "a", "map"]')
    assert tags_mod.edit_tags_as_json(album["path"]) is False
    assert album["saved"] == []


def test_scalar_and_list_values_are_accepted(album, monkeypatch):
    def edit(text, **_kw):
        doc = json.loads(text)
        doc["01.flac"]["tracknumber"] = 3
        doc["02.flac"]["artist"] = ["A", "B"]
        return json.dumps(doc)

    monkeypatch.setattr(tags_mod.click, "edit", edit)
    assert tags_mod.edit_tags_as_json(album["path"]) is True
    assert sorted(album["saved"]) == ["01.flac", "02.flac"]
