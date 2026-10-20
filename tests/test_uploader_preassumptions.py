from typing import Any, cast

import anyio

from salmon.uploader.preassumptions import print_group_info


class FakeSite:
    """An OPS-shaped torrentgroup answer: no torrent carries a "remastered" key."""

    async def torrentgroup(self, group_id):
        return {
            "group": {
                "name": "Hold Fast",
                "year": 2012,
                "recordLabel": "Fiction",
                "catalogueNumber": "CAT1",
                "musicInfo": {"artists": [{"name": "The Crookes"}]},
            },
            "torrents": [
                {"media": "CD", "format": "FLAC", "encoding": "Lossless", "remasterYear": 2012},
                {"media": "CD", "format": "MP3", "encoding": "V0 (VBR)"},
                {"media": "WEB", "format": "FLAC", "encoding": "Lossless"},
            ],
        }


def test_print_group_info_without_remastered_key(capsys):
    anyio.run(print_group_info, cast("Any", FakeSite()), 1, "CD")

    out = capsys.readouterr().out
    assert "> 2012 / Fiction / CD / FLAC / Lossless" in out
    assert "> OR / Fiction / CAT1 / CD / MP3 / V0 (VBR)" in out
    assert "WEB" not in out
