# 🔞 Adult Media Manager

**Self-hosted media organizer for adult collections — powered by ThePornDB & StashDB**

Automatically identifies, tags, and renames adult scene files using metadata from two independent databases. Scan a folder, match scenes with confidence scores and thumbnails, pick a naming template, and let the app do the rest — safely, with full undo support.

---

## ⚠️ Age Restriction

**THIS SOFTWARE IS INTENDED FOR ADULTS ONLY (18+)**

By using this software you confirm you are of legal age to access adult content in your jurisdiction. The authors accept no responsibility for misuse or violation of local laws.

---

## ✨ Features at a Glance

| | |
|---|---|
| 🎯 Smart detection | 7 filename pattern formats — parses site, date, performers, quality automatically |
| 🔍 Two databases | [ThePornDB](https://theporndb.net/) and [StashDB](https://stashdb.org/) — use one or both |
| 📝 Naming templates | 6 built-in + fully custom with variables like `{site}`, `{performer}`, `{date}` |
| 🏷️ Metadata embedding | Writes title, performers, studio, date into MP4/MKV tags via FFmpeg |
| 📄 NFO sidecars | Writes Kodi/Jellyfin-compatible `.nfo` files alongside renamed files |
| 🖱️ Drag & drop | Drop files or folders directly onto the browser window |
| ⚙️ Settings UI | Add API keys in the browser — no config file editing required |
| 🔒 Privacy mode | One-click thumbnail blurring for discretion |
| 🌍 6 languages | English, German, French, Spanish, Portuguese, Japanese |
| 📜 History & undo | Every rename is logged and fully reversible |
| 🐳 Docker | Single-container, named volume, PUID/PGID support |
| 📦 Native Linux | Self-installing AppImage + `.deb` package — no Python required |

---

## 🔑 API Keys

Two databases are supported. Both are free. You need at least one.

### ThePornDB (TPDB) — recommended

The primary and most comprehensive database.

1. Create a free account at **[theporndb.net](https://theporndb.net/)**
2. After login, click your username (top-right) → **API Keys**
3. Copy your key — it looks like: `abc123def456...`

> API access is free for personal use. A small monthly subscription unlocks higher rate limits.

### StashDB — optional, completely free

Community-maintained database with strong coverage of indie and clip-site content.

1. Register at **[stashdb.org/register](https://stashdb.org/register)**
   - **Invite code:** `3bf7c4b8-b7a6-45b8-a8a6-8b38c10b8fa6`
2. After login, click your username (top-right) → copy the **API Key**
3. It's a long JWT token starting with `eyJ...`

---

## 🐳 Quick Start — Docker

### 1. Get the files

```bash
git clone https://github.com/yourusername/adult-media-manager.git
cd adult-media-manager
```

### 2. Configure

Edit `.env` and set at minimum:

```env
AMM_PORT=8887          # port the UI will be on
PUID=1000              # your user ID: run  id -u
PGID=1000              # your group ID: run  id -g

# Optional — you can also add keys via the ⚙ Settings page in the UI
TPDB_API_KEY=your_tpdb_key_here
STASHDB_API_KEY=your_stashdb_key_here
```

### 3. Add your media path

Edit `docker-compose.yml` and add a volume for your media:

```yaml
volumes:
  - adult-media-manager-data:/data
  - /mnt/nas/videos:/mnt/nas/videos   # ← your actual path
```

> Use the **same path** inside the container as on the host — this lets you enter absolute paths in the UI without translation.

### 4. Start

```bash
docker compose up -d
```

Open **http://localhost:8887** (or your configured port).

### 5. Add API keys (alternative to editing `.env`)

Click **⚙ Settings** in the top-right of the UI. Enter your keys and click **Save**. Keys take effect immediately — no restart needed. Keys set in `.env` always take priority over saved keys.

---

## 📦 Native Linux Packages

No Docker required. Ships a self-contained Python 3.12 runtime — no system Python dependency.

### AppImage (recommended — no root required)

1. Download `Adult.Media.Manager-1.0.0.AppImage`
2. Make it executable:
   ```bash
   chmod +x Adult.Media.Manager-1.0.0.AppImage
   ```
3. Double-click it (or run it from the terminal)

**On first launch it self-installs:**
- Copies itself to `~/.local/bin/adult-media-manager.AppImage`
- Installs the icon in `~/.local/share/icons/hicolor/`
- Creates a desktop entry so it appears in your app launcher

From that point, launch it from your application menu. The original downloaded file can be deleted.

### .deb Package (Debian / Ubuntu / Mint)

```bash
sudo apt install ./adult-media-manager_1.0.0_amd64.deb
```

Launch **Adult Media Manager** from your application menu, or:

```bash
/opt/Adult\ Media\ Manager/adult-media-manager --no-sandbox
```

**API keys in native installs:** Use the **⚙ Settings** page inside the app. Keys are stored in `~/.local/share/adult-media-manager/settings.json`.

---

## 📖 Usage Guide

### Basic Workflow

```
Scan → Match → Review → Rename
```

1. **Scan** — Enter a folder path (or drag & drop files/folders onto the window). Enable **Recursive** to include subfolders. Click **Scan**.

2. **Match** — Select a datasource (TPDB or StashDB) and click **Match**. The app searches the database and shows confidence scores, thumbnails, and performers for each file.

3. **Review** — Check matches. Files already organised (detected via `.nfo` sidecars) are shown in a collapsed section and skipped by default.

4. **Choose a template** — Pick from the presets or enter a custom template.

5. **Rename** — Choose an action and click **Rename**:
   - **TEST** — Preview output without touching any files *(always try this first)*
   - **MOVE** — Move files to the new location
   - **COPY** — Copy files, leaving originals in place
   - **HARDLINK** — Space-efficient links (same filesystem only)
   - **SYMLINK** — Symbolic links

After rename, FFmpeg embeds metadata (title, performers, studio, release date) into MP4/MKV files in the background. A `.nfo` sidecar is written alongside each file for Kodi/Jellyfin/Plex compatibility.

### Drag & Drop

Drop files or folders directly onto the browser window at any time.

- **Native app (AppImage/deb):** real filesystem paths are read directly — works for any location on your system.
- **Docker/browser:** the path shown is the browser's virtual path. Edit it to match the actual container mount path if needed (e.g. `/mnt/nas/videos/folder`), then click **Scan**.

### Settings

Click **⚙** (top-right) to open Settings. Each API key row shows its current status:

- 🟢 **Saved** — key stored in the app's settings file
- 🟣 **Set via environment** — key comes from `.env` / shell environment (read-only in UI)
- ⚪ **Not configured** — no key set

### Privacy Mode

Click the **🔒** button to blur all thumbnails. The setting persists across sessions. Individual thumbnails can be revealed by clicking them.

---

## 📝 Naming Templates

### Built-in Templates

| Template | Example output |
|---|---|
| Site-Focused | `Brazzers/2024/Brazzers.2024-01-15.Hot.Scene.1080p.mp4` |
| Performer-Focused | `Jane Doe/Brazzers.2024-01-15.Hot.Scene.1080p.mp4` |
| Studio-Organised | `Brazzers/Jane Doe/2024-01-15.Hot.Scene.mp4` |
| Simple | `Jane Doe - Hot Scene (Brazzers).mp4` |
| Multi-Performer | `Brazzers/Jane Doe, John Smith/2024-01-15.Hot.Scene.mp4` |
| Dated Folders | `2024/01/Brazzers.Jane.Doe.Hot.Scene.mp4` |

### Template Variables

| Variable | Description | Example |
|---|---|---|
| `{site}` | Site / studio name | `Brazzers` |
| `{performer}` | First performer | `Jane Doe` |
| `{performers}` | All performers, comma-separated | `Jane Doe, John Smith` |
| `{scene}` | Scene title | `Hot Scene` |
| `{date}` | Full date YYYY-MM-DD | `2024-01-15` |
| `{year}` | Year | `2024` |
| `{month}` | Month (zero-padded) | `01` |
| `{day}` | Day (zero-padded) | `15` |
| `{quality}` | Resolution label | `1080p`, `4K` |
| `{vf}` | Video codec | `x264`, `HEVC` |
| `{source}` | Source type | `WEB-DL` |
| `{group}` | Release group | `XLF` |
| `{ext}` | File extension | `mp4`, `mkv` |

---

## 🔒 Privacy & Security

- **Thumbnail blurring** — toggleable per-session, off by default
- **API keys** — stored in `.env` (Docker) or `~/.local/share/adult-media-manager/settings.json` (native); never returned in API responses
- **Path validation** — every path is checked against an allowlist of permitted roots before any file operation; paths outside the allowlist are rejected with 403
- **NAS / FUSE safety** — metadata embedding uses a 3-phase commit: FFmpeg writes to a local staging area, the result is verified, then atomically swapped into place — no partial writes land on your network share
- **Do not commit `.env`** — it contains your API keys; add it to `.gitignore` if you fork this repo

---

## 🗂️ REST API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan` | Scan a directory or comma-separated file list |
| `POST` | `/api/match` | Match scanned files against TPDB / StashDB |
| `POST` | `/api/rename` | Execute rename (test / move / copy / hardlink / symlink) |
| `GET` | `/api/embed-status/{job_id}` | Poll background metadata embed progress |
| `GET` | `/api/history` | List rename history |
| `POST` | `/api/history/undo` | Undo last rename |
| `GET` | `/api/browse` | Server-side directory browser |
| `GET` | `/api/templates` | List available naming templates |
| `GET` | `/api/settings` | Get API key status (values are never returned) |
| `POST` | `/api/settings` | Save API keys |
| `GET` | `/api/health` | Health check |

---

## ⚙️ Environment Variables (Docker)

| Variable | Default | Description |
|---|---|---|
| `TPDB_API_KEY` | *(blank)* | ThePornDB API key |
| `STASHDB_API_KEY` | *(blank)* | StashDB API key |
| `AMM_PORT` | `8887` | Host port for the web UI |
| `PUID` | `1000` | User ID for file ownership |
| `PGID` | `1000` | Group ID for file ownership |
| `PRIVACY_MODE` | `false` | Start with thumbnails blurred |
| `DATA_DIR` | `/data` | Persistent data directory (history, settings, embed staging) |

---

## 🐛 Troubleshooting

**"Path not found" or "Access denied" on scan**
- **Docker:** check the path is mounted in `docker-compose.yml` using the exact same path inside and outside the container.
- **Native:** allowed roots are `$HOME`, `~/Videos`, `~/Movies`, `~/Downloads`, `/run/media`, `/srv`, `/nas`, `/storage`. Paths outside these are rejected.

**Drag & drop shows wrong path (Docker)**
- The browser reports its virtual path (e.g. `/VideoFile.mp4`). Edit the scan path field to the actual container mount path and click **Scan**.

**Metadata not embedding**
- FFmpeg must be installed. Docker bundles it. The `.deb` package lists it as a dependency. For the AppImage, install FFmpeg on your system: `sudo apt install ffmpeg`.

**No results from TPDB / StashDB**
- Verify your API key in **⚙ Settings** — the badge shows whether the key is saved or env-managed.
- Test connectivity from Docker: `docker exec adult-media-manager curl -s https://theporndb.net/api/health`

**Permission denied on renamed files**
- Set `PUID`/`PGID` in `.env` to match your host user (`id -u` / `id -g`).

**Container won't start**
- Check logs: `docker compose logs -f`

---

## 📄 License

MIT — see [LICENSE](LICENSE).

**Age restriction:** For adults (18+) only. Use only with legally obtained content. Always respect content creator rights.

---

## 🙏 Acknowledgements

- [ThePornDB](https://theporndb.net/) — adult content metadata API
- [StashDB](https://stashdb.org/) — community-maintained adult scene database
- [FastAPI](https://fastapi.tiangolo.com/) — Python web framework
- [Electron](https://www.electronjs.org/) — native Linux packaging
- [FFmpeg](https://ffmpeg.org/) — metadata embedding and transcoding
