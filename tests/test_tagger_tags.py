"""Vorbis comments as salmon reads and writes them: a repeated value read once, names written in uppercase."""

import struct

from mutagen.flac import FLAC, VCFLACDict

from salmon.tagger.retagger import Change, retag_files
from salmon.tagger.tagfile import TagFile
from salmon.tagger.tags import standardize_tags


def _write_flac(path, **tags: str) -> None:
    """A FLAC with no audio, just a STREAMINFO block, tagged with comments in the case each keyword has."""
    streaminfo = struct.pack(">HH", 4096, 4096) + bytes(6)
    streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big") + bytes(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    if tags:
        tagged = FLAC(path)
        for key, value in tags.items():
            tagged[key] = value
        tagged.save()


def test_flac_tag_repeated_with_the_same_value_reads_once(tmp_path):
    """Some store FLACs carry TRACKNUMBER=1 twice; it reads as "1"."""
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["tracknumber"] = ["1", "1"]
    tagged["isrc"] = ["GBABC2600001", "GBABC2600001"]
    tagged.save()

    tag_file = TagFile(str(tmp_path / "01.flac"))

    assert tag_file.tracknumber == "1"
    assert tag_file.isrc == "GBABC2600001"


def test_flac_artists_and_genres_repeated_with_the_same_value_read_once(tmp_path):
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["artist"] = ["Artist", "Guest", "Artist"]
    tagged["genre"] = ["Rock", "Rock"]
    tagged.save()

    tag_file = TagFile(str(tmp_path / "01.flac"))

    assert tag_file.artist == ["Artist", "Guest"]
    assert tag_file.genre == ["Rock"]


def test_flac_tag_with_repeated_and_different_values_keeps_each_once_in_order(tmp_path):
    _write_flac(tmp_path / "01.flac")
    tagged = FLAC(tmp_path / "01.flac")
    tagged["comment"] = ["second", "first", "second"]
    tagged.save()

    comment = TagFile(str(tmp_path / "01.flac")).comment

    assert comment == "second; first"


def test_standardize_tags_writes_uppercase_vorbis_comment_keys(tmp_path):
    """Each name is written uppercase, as the Vorbis comment spec recommends, aliases included."""
    _write_flac(tmp_path / "01.flac", YEAR="1999", TOTALTRACKS="12", ARTIST="Artist", ENCODER="reference")
    _write_flac(tmp_path / "02.flac", year="2001", TITLE="Song")

    standardize_tags(str(tmp_path))

    first = FLAC(tmp_path / "01.flac")
    comments = first.tags
    assert isinstance(comments, VCFLACDict)
    # Iterate the comment, not .keys(): that normalizes every name to lowercase.
    assert {key for key, _value in comments} == {"DATE", "TRACKTOTAL", "ARTIST", "ENCODER"}
    assert first["date"] == ["1999"]
    assert first["tracktotal"] == ["12"]
    second = FLAC(tmp_path / "02.flac")
    second_comments = second.tags
    assert isinstance(second_comments, VCFLACDict)
    assert {key for key, _value in second_comments} == {"DATE", "TITLE"}
    assert second["date"] == ["2001"]


def test_retag_files_writes_uppercase_vorbis_comment_keys(tmp_path):
    """Each retagged name is written uppercase, as the Vorbis comment spec recommends."""
    _write_flac(
        tmp_path / "01.flac",
        ARTIST="Old Artist",
        title="Old Title",
        ALBUM="Old Album",
        CATALOGNUMBER="OLD",
        ENCODER="reference",
    )

    retag_files(
        str(tmp_path),
        {"album": "New Album", "albumartist": "Album Artist", "catno": "CAT-1"},
        {"01.flac": [Change("title", "Old Title", "New Title"), Change("artist", "Old Artist", "New Artist")]},
    )

    audio = FLAC(tmp_path / "01.flac")
    comments = audio.tags
    assert isinstance(comments, VCFLACDict)
    assert {key for key, _value in comments} == {"TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "CATALOGNUMBER", "ENCODER"}
    assert audio["title"] == ["New Title"]
    assert audio["artist"] == ["New Artist"]
    assert audio["catalognumber"] == ["CAT-1"]
