# AGENTS.md

smoked-salmon: async CLI for uploading to Gazelle-based music trackers (RED/OPS/DIC). Python 3.13, managed with uv.

## Commands
- Setup: `uv sync`
- Lint: `uv run ruff check --fix`
- Format: `uv run ruff format`
- Type check: `uv run basedpyright`
- Tests: `uv run pytest`

## Testing gotcha
Importing `salmon` eagerly loads config (`src/salmon/__init__.py:8` calls `setup_config()`, exits on failure).
All tests do `from salmon import cfg`, so pytest fails/hangs without a valid `config.toml`. None exists in the
repo (gitignored). Create one at the repo root (preferred over `~/.config/smoked-salmon/config.toml`) by copying
`src/salmon/data/config.default.toml`. Note `config/validations.py` rejects configs whose
`directory.download_directory` / `dottorrents_dir` are not real directories.

## Live API testing (read-only metadata only)
- Root `.env` (gitignored) stores real API credentials. The app
  does not auto-load it; source it yourself (`set -a; source .env; set +a`). Never commit it or print secrets.
- ALLOWED: read-only metadata API calls to verify code — Tidal, Qobuz, Discogs, Beatport, Apple Music,
  MusicBrainz, Bandcamp, Deezer (exercising `sources/*`, `search/*`, `tagger/sources/*`, or `salmon descgen`).
- FORBIDDEN: any upload path — no `up`/`specs`/`checkspecs`, no tracker (RED/OPS/DIC) or image-host requests,
  no torrent creation, seedbox transfers, or torrent-client injection. Tracker diagnostics like
  `checkconf` (without `-m`) are off-limits too; `checkconf -m` (metadata sources only) is allowed.

## Architecture
- CLI entry: `src/salmon/run.py` (`salmon.run:main`); imports subpackages for command registration.
- Commands live in `commands.py`, `uploader/__init__.py` (up), `tagger/__init__.py`, `converter/__init__.py`.
- Config: TOML via msgspec; schema + validation in `config/validations.py` (`__post_init__` on Structs).
- Metadata providers are mirrored in 3 module families, one file each: `sources/` (fetch), `search/`,
  `tagger/sources/` (apple_music, bandcamp, beatport, deezer, discogs, musicbrainz, qobuz, tidal).
- Trackers in `trackers/` (red/ops/dic) share Gazelle logic in `trackers/base.py`; spectral viewer in `web/`.

## Conventions
- Ruff line-length 120
