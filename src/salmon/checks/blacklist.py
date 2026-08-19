"""RED upload blacklist — hard-blocks releases RED prohibits (its 'Do Not Upload' list)."""

import re
import tomllib
from functools import cache
from importlib.resources import files


def _tokens(value) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


@cache
def _entries() -> list[dict]:
    raw = files("salmon.data").joinpath("red_blacklist.toml").read_text(encoding="utf-8")
    return tomllib.loads(raw).get("entry", [])


def _describe(entry: dict) -> str:
    if entry.get("label"):
        subject = f'label "{entry["label"]}"'
    elif entry.get("album"):
        subject = f"{entry['artist']} — {entry['album']}"
    else:
        subject = f"{entry['artist']} (entire discography)"
    return f"{subject} is on RED's Do-Not-Upload list. {entry.get('note', '')}".strip()


def red_blacklist_reason(artists, album, label=None) -> str | None:
    """Return a block reason if this release is on RED's blacklist, else None.

    artists: list of names or (name, importance) tuples; album/label: strings.
    Every word of an entry's artist (and album, if set) must appear in the
    release for it to match, so partial-word false positives don't fire.
    """
    artist_token_sets = [_tokens(a[0] if isinstance(a, list | tuple) else a) for a in artists or []]
    album_tokens = _tokens(album)
    label_tokens = _tokens(label)

    for entry in _entries():
        if entry.get("label"):
            if label_tokens and _tokens(entry["label"]) <= label_tokens:
                return _describe(entry)
            continue
        # Match the entry artist against each individual release artist by equality, so a
        # blacklisted "Viper" doesn't also block "Viper UK" while still catching collaborations.
        entry_artist = _tokens(entry["artist"])
        if not any(entry_artist == artist for artist in artist_token_sets):
            continue
        if entry.get("album") and not (_tokens(entry["album"]) <= album_tokens):
            continue
        return _describe(entry)
    return None
