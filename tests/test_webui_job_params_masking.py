"""The web UI shows a job's params with every URL among them masked: a pasted link can carry an authkey or passkey."""

import pytest

from salmon.common.redaction import redact_url
from salmon.webui.jobs import Job

PASSKEY = "abcdef0123456789abcdef0123456789"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://tracker.example/torrents.php?action=download&id=12&authkey=test-authkey-0001&torrent_pass=t1",
            "https://tracker.example/torrents.php?action=download&id=12&authkey=[REDACTED]&torrent_pass=[REDACTED]",
        ),
        (
            "https://tracker.example/torrents.php?id=34&torrentid=56",
            "https://tracker.example/torrents.php?id=34&torrentid=56",
        ),
        (f"https://tracker.example/{PASSKEY}/announce", "https://tracker.example/[REDACTED]/announce"),
        ("https://user:test-password@tracker.example:8443/x", "https://tracker.example:8443/x"),
        (
            "https://store.example/album/the-album-deluxe-edition#token=t2",
            "https://store.example/album/the-album-deluxe-edition#[REDACTED]",
        ),
        (f"https://tracker.example/torrents.php?id={PASSKEY}", "https://tracker.example/torrents.php?id=[REDACTED]"),
        ("https://[::1]:8080/a?sig=s3", "https://[::1]:8080/a?sig=[REDACTED]"),
        ("https://tracker.example:bad/x", "[REDACTED]"),
    ],
    ids=["query-secrets", "ids-kept", "passkey-path", "userinfo", "fragment", "long-id", "ipv6", "unparseable"],
)
def test_a_url_is_shown_without_its_secrets(url: str, shown: str) -> None:
    masked = redact_url(url)

    assert masked == shown


def test_a_jobs_params_mask_every_url_and_keep_the_rest() -> None:
    link = "https://tracker.example/torrents.php?torrentid=7&torrent_pass=test-pass-0001"
    params = {
        "path": link,
        "source": "RED",
        "trackers": ["RED", link],
        "nested": {"source_url": link},
        "group": 9,
    }
    job = Job("cross-upload", "Cross-upload RED -> OPS", params)

    shown = job.to_dict()["params"]

    masked = "https://tracker.example/torrents.php?torrentid=7&torrent_pass=[REDACTED]"
    assert shown == {
        "path": masked,
        "source": "RED",
        "trackers": ["RED", masked],
        "nested": {"source_url": masked},
        "group": 9,
    }
    assert job.params["path"] == link, "the job itself keeps what it was given"


def test_a_local_path_is_shown_as_it_is() -> None:
    job = Job("upload", "Upload: Album", {"path": "/downloads/Artist - Album (2020) [FLAC]"})

    shown = job.to_dict()["params"]

    assert shown == {"path": "/downloads/Artist - Album (2020) [FLAC]"}


def test_a_url_too_malformed_to_parse_is_masked_whole() -> None:
    job = Job(
        "cross-upload", "Cross-upload RED -> OPS", {"path": "https://tracker.example:bad/x?authkey=test-authkey-0002"}
    )

    shown = job.to_dict()["params"]

    assert shown == {"path": "[REDACTED]"}
