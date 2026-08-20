[![Alpha Image](https://github.com/chodeus/smoked-salmon/actions/workflows/build-alpha.yml/badge.svg)](https://github.com/chodeus/smoked-salmon/actions/workflows/build-alpha.yml) [![Tests](https://github.com/chodeus/smoked-salmon/actions/workflows/test.yml/badge.svg?branch=master)](https://github.com/chodeus/smoked-salmon/actions/workflows/test.yml) [![Linting](https://github.com/chodeus/smoked-salmon/actions/workflows/lint.yml/badge.svg?branch=master)](https://github.com/chodeus/smoked-salmon/actions/workflows/lint.yml)

# 🐟 smoked-salmon  

A simple tool to take the work out of uploading on Gazelle-based trackers. It generates spectrals, gathers metadata, allows re-tagging/renaming files, and automates the upload process.

> **About this fork** — upstream's release pipeline stalled at 0.10.1 while fixes piled up unreleased. This fork is upstream `master` plus the outstanding community fix branches and a batch of our own work, reviewed and covered by tests.
>
> **Images:** `ghcr.io/chodeus/smoked-salmon` — `:alpha` is built on every push to `master`, `:latest` on release.
>
> **What this fork adds on top of upstream master**
> - A full browser interface (`salmon web`) behind a shared-secret token, with inline spectrals and interactive prompts.
> - Per-tracker image hosts, per-tracker seedbox destinations, and site-aware upload rules (path limits, bit-depth/sample-rate policy).
> - A RED do-not-upload blacklist that blocks matching releases before anything is sent to RED.
> - `--dry-run`, RED↔OPS cross-upload, and a single-mount (`/config` + `/data`) container layout.
> - Fixes for upstream issues #353, #356, #358, #429, #430, #432, #433, plus Apple Music / Tidal repairs and multi-disc log handling.
>
> **Defaults that differ from upstream:** the recent-uploads check is **off** (#432 — it could flood `login.php` and get an IP firewalled), and image hosts default to keyless **catbox** instead of the defunct ptpimg.

## 🌟 Features  

- **Interactive Uploading** – Supports **multiple trackers** (RED / OPS / DIC), from the CLI or the web interface.
- **Web Interface** – `salmon web` serves the whole workflow in a browser: uploads, checks, transcodes, metadata search and spectral review, gated by a shared-secret token.
- **Log Checking** – Calculates log scores, verifies log checksum integrity, and validates log-to-FLAC file matching.
- **Upconvert Detection** – Checks 24-bit flac files for potential upconverts.
- **MQA Detection** – Checks files for common MQA markers.
- **Duplicate Upload Detection** – Prevents redundant uploads.  
- **Blacklist Enforcement** – A release on RED's do-not-upload list is blocked before anything is sent to RED; other trackers in the same run continue.  
- **Dry Run** – `--dry-run` builds and validates a complete upload without posting it.  
- **Spectral Analysis** – Generates, compresses, and verifies spectrals, shown inline in the web interface.  
- **Spectral Upload** – Can generate spectrals for an existing upload (based on local files), and update the release description.  
- **Lossy Master Report Generation** – Supports lossy master reports during upload.
- **Metadata Retrieval** – Fetches metadata from:
  - Apple Music, Bandcamp, Beatport, Deezer, Discogs, MusicBrainz, Qobuz, Tidal.
- **File Management** –  
  - Retags and renames files to standard formats (based on metadata).
  - Checks file integrity and sanitizes if needed.  
- **Request Filling** – Scans for matching requests on trackers.
- **Description generation** – Edition description generation (tracklist, sources, available streaming platforms, encoding details...).
- **Down-convert and Transcode** – Can downconvert 24-bit flac files to 16-bit, and transcode to mp3.
- **Multi-Format Upload** – Automatically transcodes and uploads multiple formats (FLAC 16-bit, MP3, etc.) in a single workflow.
- **Per-Tracker Image Hosts** – Use a tracker's own image host for its covers and a neutral host elsewhere. Spectrals are never sent to RED's host.
- **Site-Aware Rules** – Path-length limits and bit-depth/sample-rate policy applied per tracker, since RED and OPS differ.
- **Cross-Upload** – Copy an existing upload from one tracker to another, rehosting images where the target requires it.
- **Torrent Client Injection** – Can inject generated torrent files into torrent clients (qBittorrent, Transmission, Deluge, ruTorrent).
- **Remote Seeding** – Can transfer files to remote locations via rclone and inject torrents into remote clients for automatic seeding. Each destination can be pinned to specific trackers, so a tracker's uploads land in its own directory and category.
- **Update Notifications** – Informs users when a new version is available.

## 📥 Installation  

Manual installation instructions can be found on the [Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Installation).

### 🔹  Install smoked-salmon 
These steps use [`uv`](https://github.com/astral-sh/uv) for installing the *smoked-salmon* package. [`pipx`](https://github.com/pypa/pipx) also works.
Installing with pip is not recommended because uv (and pipx) manage python versions and isolate the *smoked-salmon* installation from the system python installation.

#### Linux
1. Install system packages:
    ```bash
    sudo apt install sox libsox-fmt-mp3 flac mp3val curl lame rclone git
    ```

    `libsox-fmt-mp3` is required for spectrals of MP3 releases — Debian's `sox` package cannot read MP3 without it, and fails silently. `metaflac` comes with `flac`. `rclone` is only needed for remote seeding, and `git` for the install step below.

    Optional: `feh` for the native spectral viewer, `puddletag` if you set `prompt_puddletag`.

2. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

3. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/chodeus/smoked-salmon
	```

#### Windows
1. Install required system packages using winget:
    ```powershell
    winget install -e ChrisBagwell.SoX Xiph.FLAC LAME.LAME ring0.MP3val.WF Git.Git
    ```

    Add `Rclone.Rclone` as well if you plan to use remote seeding.

2. Fix sox Unicode filename handling issue on Windows:
    ```powershell
    $soxDir = $((Get-Command sox).Source | Split-Path)
    $zipPath = Join-Path -Path $soxDir -ChildPath "sox_windows_fix.zip"
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/DevYukine/red_oxide/master/.github/dependency-fixes/sox_windows_fix.zip" -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $soxDir -Force
    regedit "$soxDir\PreferExternalManifest.reg"
    Remove-Item $zipPath
    ```

3. Install uv:
    ```powershell
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    ```

4. Install smoked-salmon package from github:
	```powershell
	uv tool install git+https://github.com/chodeus/smoked-salmon
	```

#### macOS
1. Install Homebrew (if you haven't already):
    ```bash
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ```

2. Install system packages using Homebrew:
    ```bash
    brew install sox flac mp3val curl lame rclone git
    ```

    Homebrew's `sox` already handles MP3. `rclone` is only needed for remote seeding.

3. Install uv:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

4. Install smoked-salmon package from github:
	```bash
	uv tool install git+https://github.com/chodeus/smoked-salmon
	```

### 🔹  Initial Setup
1. Run salmon for the first time and follow the instructions to create a default configuration:
	```
	salmon-user@salmon:~$ salmon
	Could not find configuration path at /home/salmon-user/.config/smoked-salmon/config.toml.
	Do you want smoked-salmon to create a default config file at /home/salmon-user/.config/smoked-salmon/config.toml? [y/N]:
	```

2. Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

3. Use the `checkconf` command to verify that the connection to the trackers is working:

	```
	salmon checkconf
	```

4. Use the `health` command to verify that all necessary command line dependencies are installed:
	```
	salmon health
	```

### 🐳 Docker Installation

A Docker image is built on every push to `master` (`:alpha`) and on release (`:latest`). This fork is developed and run as a container, so the instructions below are the maintained path.

1. Pull the image:

   ```bash
   # Stable release
   docker pull ghcr.io/chodeus/smoked-salmon:latest

   # Alpha (built on every push to master, equivalent to `uv tool install git+...`)
   docker pull ghcr.io/chodeus/smoked-salmon:alpha
   ```

   > The examples below use the `latest` tag. Replace with `alpha` to use the latest development version.

2. Copy the content of the file [`config.toml`](https://github.com/chodeus/smoked-salmon/blob/master/src/salmon/data/config.default.toml) to a location on your host server.
   Edit the `config.toml` file with your preferred text editor to add your API keys, session cookies and update your preferences (see the [Configuration Wiki](https://github.com/smokin-salmon/smoked-salmon/wiki/Configuration)).

3. Configure rclone only if you use the remote-seeding features: put `rclone.conf` in the same `/config` volume and set `RCLONE_CONFIG=/config/rclone.conf`. Run `rclone config file` on your host to find your existing one.

---

### 🔁 Docker Usage

1. **Check Configuration**
   Run the container with the `checkconf` command to verify that the connection to the trackers is working:

   ```bash
   docker run --rm -it --network=host \
   -v /path/to/your/config/directory:/config \
   -v /path/to/your/data:/data \
   ghcr.io/chodeus/smoked-salmon:latest checkconf
   ```

2. **Upload**
   Run the upload command directly (replace `checkconf` with any salmon command):

   ```bash
   docker run --rm -it --network=host \
   -v /path/to/your/config/directory:/config \
   -v /path/to/your/data:/data \
   ghcr.io/chodeus/smoked-salmon:latest up "/data/path/to/album" -s WEB
   ```

> **Container paths.** The image sets `SALMON_CONFIG_DIR=/config`, so it reads
> `/config/config.toml` — two mounts are enough. Point `rclone` at the same
> volume with `-e RCLONE_CONFIG=/config/rclone.conf`, and set `tmp_dir`,
> `download_directory` and `dottorrents_dir` in `config.toml` rather than
> adding more mounts. Upgrading from an image that read
> `/root/.config/smoked-salmon/`: move `config.toml` into the `/config` volume,
> otherwise salmon will not find it and will exit on startup.

### 💡 Shell Alias (Optional)

To avoid repeating the long `docker run` command, add the following alias to your shell configuration file (`~/.bashrc`, `~/.zshrc`, etc.):

```bash
alias salmon='docker run --rm -it --network=host \
  -v /path/to/your/config/directory:/config \
  -v /path/to/your/data:/data \
  ghcr.io/chodeus/smoked-salmon:latest'
```

Then use it just like a native install:

```bash
salmon checkconf
salmon health
salmon up "/data/path/to/album" -s WEB
```

---

### ⚠️ Notes

- **Permission Issues**  
  The container currently **able to handle permissions** properly.  
  If your torrent client is not run as root, or if new uploads are inaccessible, you may need to:
  - Manually adjust file/folder ownership (`chown`) or permissions (`chmod`)
  - Ensure the container and torrent client users are compatible
  - Optionally run containers with matching `--user` flags or add `umask` logic
     ```bash
    user: "1001:100"
    environment:
      - PUID=1001
      - PGID=100
     ```

- **Directory Settings**  
  `download_directory`, `dottorrents_dir` and `tmp_dir` are set in `config.toml`. Point them at paths inside the `/data` volume (and `/config` for scratch) rather than adding a bind mount for each one — for example:

  ```toml
  [directory]
  download_directory = "/data/torrents/salmon"
  dottorrents_dir = "/data/torrents/salmon/.torrents"
  tmp_dir = "/config/tmp"
  ```

  Keeping `download_directory` on the same volume as your library lets salmon hardlink instead of copying.

- **Uploading from a curated library**  
  By default salmon expects to be pointed at a disposable download: it retags the folder in place, and aborting an upload can delete it. If you upload from a collection you want left alone (a Lidarr library, say), list it under `library_dirs`:

  ```toml
  [directory]
  library_dirs = ["/data/media/music"]
  ```

  Those folders become browsable in the web interface, are never deleted, and are copied into `download_directory` before anything touches them. The copy is deliberate rather than a hardlink — a hardlink shares the inode, so retagging would write straight back into your library.

- **rclone Configuration**  
  Only needed for the remote-seeding features. Put `rclone.conf` in the `/config` volume and point rclone at it with an environment variable, so no extra mount is required:

  ```bash
  -e RCLONE_CONFIG=/config/rclone.conf
  ```

---

### 📦 Docker Compose

If using Docker Compose, create a `docker-compose.yml` to define your volume mappings and network settings, then use `docker compose run` to execute any salmon command on demand:

```yaml
services:
  salmon:
    image: ghcr.io/chodeus/smoked-salmon:latest
    network_mode: host
    environment:
      - RCLONE_CONFIG=/config/rclone.conf   # Optional: only if using rclone features
    volumes:
      - /path/to/your/config/directory:/config
      - /path/to/your/data:/data

```

```bash
# Check configuration
docker compose run --rm salmon checkconf

# Upload
docker compose run --rm salmon up "/data/path/to/album" -s WEB
```

## 🚀 Usage

### 🎨 Terminal Colors
smoked-salmon uses distinct terminal colors for different types of messages:

* Default – General information
* Red – Errors or critical failures
* Green – Success messages
* Yellow – Information headers
* Cyan – Section headers
* Magenta – User prompts

### 🔧 CLI Mode
smoked-salmon can be driven entirely from the CLI, or from the browser (see below). Quick start usage instructions can be found on the [Wiki Usage page](https://github.com/smokin-salmon/smoked-salmon/wiki#usage).

The examples below show how to run smoked-salmon directly. If you're using Docker, you'll need to adjust them accordingly, but the underlying principles remain the same.

To see the available commands, just type:
```bash
salmon
```

To test the connection to the trackers, run:
```bash
salmon checkconf
```

To check the status of salmon's command line and config dependencies, run:
```bash
salmon health
```

To start an upload (with the WEB source):
```bash
salmon up /data/path/to/album -s WEB
```

To rehearse an upload without posting anything — everything is gathered, checked and built, then stopped at the last step:
```bash
salmon up /data/path/to/album -s WEB --dry-run
```

To start the web interface:
```bash
salmon web --host 0.0.0.0
```

You can get help directly from the CLI by appending --help to any command. This is especially useful for the up command which has a lot of possible options.

### 🌐 Web Interface
`salmon web` serves the whole workflow in a browser — uploads, checks, transcodes, metadata search and spectral review — on port **55155** by default:

```bash
salmon web --host 0.0.0.0
```

Prompts that the CLI would ask on the terminal appear in the browser instead, and spectrals are shown inline as they are generated.

Because the interface holds your tracker session cookies and can trigger real uploads, protect it with a shared secret whenever it is reachable beyond loopback. Set `SALMON_WEB_TOKEN` (or `auth_token` under `[upload.web_interface]`); leaving it unset disables the gate entirely.

> The older standalone spectral viewer still exists for `salmon up`, but it binds loopback inside the process, so publishing port 55110 from a container has no effect — the web interface above supersedes it.

## 🔄 Updating

For **normal installs**:
```bash
uv tool update salmon
```

For **manual installs**:
```bash
cd smoked-salmon
git pull
uv sync
```

For **Docker users**:
```bash
docker pull ghcr.io/chodeus/smoked-salmon:alpha   # or :latest
```

## 📞 Support
For bug reports and feature requests specific to this fork, use [its GitHub Issues](https://github.com/chodeus/smoked-salmon/issues). For anything that also affects upstream, [smokin-salmon/smoked-salmon](https://github.com/smokin-salmon/smoked-salmon/issues) or the tracker forums are the better home.


## 🎭 Testimonials
```
"Salmon filled the void in my heart. I no longer chase after girls." ~boot
"With the help of salmon, I overcame my addiction to kpop thots." ~b
"I warn 5 people every day on the forums using salmon!" ~jon
```

## 🎩 Credits
* Originally created by [ligh7s](https://github.com/ligh7s/smoked-salmon). Huge thanks!
* Further development & maintenance by elghoto, xmoforf, miandru, redusys, kyokomiki and others. Keeping the dream alive.
* Docker image build workflow and update notification mechanisms heavily inspired from the awesome work of Audionut on his [Upload Assistant tool](https://github.com/Audionut/Upload-Assistant) !
* This fork carries community fix branches from AKarp123, SomeCrab163, styx-techno, Constrat and calliah333 — thanks to all of them for work upstream had not released.
