# Savepoint — Design

Self-hosted game save sync and versioned backup for Windows PCs.

Two repos:

| Repo | Contents |
|---|---|
| `savepoint-server` | FastAPI server, SQLite, blob storage, web UI, Docker deploy |
| `savepoint` | Windows tray app (.NET 8), shared sync engine, Playnite plugin (later) |

This document is the shared spec. The **API contract** section is the boundary between the two repos. Keep a copy of this file in both repos at `docs/DESIGN.md`, and update both when the contract changes.

---

## 1. Goals

- Sync save files for selected games between multiple Windows machines (currently a gaming PC and a laptop).
- Keep a full version history of every save, browsable and restorable from a password-protected web UI.
- Work from anywhere: the server is publicly reachable over HTTPS, not LAN-only.
- Work offline: changes queue locally and sync when the server is reachable again.
- Never lose data. Every overwrite is preceded by a snapshot, and conflict losers are kept as versions.

### Non-goals

- Syncing game installs.
- Automatic discovery of games or save locations. Games are added manually.
- Multi-user support. There is one user, with one admin password and one API key per machine.

### Context

- Most games are not run through launchers, so there are no cloud saves to rely on.
- Many save to emulator folders shared across games (e.g. `C:\Users\Public\Documents\Steam\CODEX\<appid>`, `%APPDATA%\Goldberg SteamEmu Saves\<appid>`, `%APPDATA%\GSE Saves\<appid>`). Others use normal locations (`Documents\My Games`, `%APPDATA%`, `%LOCALAPPDATA%`, `Saved Games`).
- The server runs on the 8hzwanip homelab Docker stack.

---

## 2. Core concepts

**Game.** A named entry with one or more save roots, the exe name(s) to watch, exclude patterns, sync mode and retention policy. Games are defined on the server, which is the master copy, and all machines see the same list.

**Save root.** A folder holding saves for a game. Each root has an alias (e.g. `saves`, `config`) and a path template. Templates use variables, and each machine can override a root's path.

Supported path variables:

| Variable | Resolves to |
|---|---|
| `%APPDATA%` | Roaming AppData |
| `%LOCALAPPDATA%` | Local AppData |
| `%LOCALLOW%` | `AppData\LocalLow` |
| `%USERPROFILE%` | User profile folder |
| `%DOCUMENTS%` | Documents, resolved via known-folder API (handles OneDrive redirection) |
| `%SAVEDGAMES%` | Saved Games known folder |
| `%PUBLIC%` | `C:\Users\Public` |

Absolute paths are also allowed.

**Manifest.** A list of `{path, hash, size, mtime}`. Each `path` is `<root alias>/<relative path>` with forward slashes, and `hash` is the SHA-256 of the raw file content.

**Blob.** A file's content, stored once per unique hash.

**Version.** An immutable snapshot of a game's saves: a manifest plus metadata (type, machine, time, tags, note, pinned). Versions only store blobs the server doesn't already have, so a full snapshot costs only its changed files.

Version types:

| Type | Trigger |
|---|---|
| `session` | The game's exe closed |
| `checkpoint` | The file watcher detected changes (debounced) |
| `manual` | User clicked "Back up now" |
| `offline` | Local changes found at startup that were never uploaded |
| `pre_restore` | Local state captured just before an overwrite, when it differed from the last sync |
| `promoted` | A copy of an older version made head from the web UI (rollback) |

**Head.** A pointer per game to the current version, which is what machines sync to. Normally this is the newest version. In a conflict, the losing version is stored but not made head.

---

## 3. Architecture

```
┌──────────── Windows machine (PC / laptop) ────────────┐
│  Savepoint.Agent (tray app)                           │
│   ├─ Exe monitor (process polling)                    │
│   ├─ File watcher (per save root, debounced)          │
│   ├─ Sync engine (Savepoint.Core)                     │
│   ├─ Local state (SQLite, %LOCALAPPDATA%\Savepoint)   │
│   └─ Local IPC (named pipe) ◄── Playnite plugin       │
└───────────────────────┬───────────────────────────────┘
                        │ HTTPS, Bearer API key
                        ▼
        Nginx Proxy Manager (savepoint.8hzwanip.com)
                        │
┌───────────────────────▼───────────────────────────────┐
│  savepoint-server (Docker)                            │
│   ├─ FastAPI: /api/v1 (clients), web UI (cookie auth) │
│   ├─ SQLite: games, machines, versions, tags          │
│   ├─ Blob store: /data/blobs (zstd-compressed)        │
│   ├─ Retention + GC job (nightly)                     │
│   └─ Optional offsite: rclone → Google Drive (nightly)│
└───────────────────────────────────────────────────────┘
```

---

## 4. API contract (`/api/v1`)

### Authentication

- **Clients** send `Authorization: Bearer <api_key>` on every request. Each key belongs to one machine.
- **Web UI** uses a session cookie after password login.
- Every endpoint accepts either form of auth, except machine and key management, which needs the session cookie.
- Requests that use the session cookie and change something (`POST`, `PUT`, `PATCH`, `DELETE`) must also send the page's CSRF token in an `X-CSRF-Token` header. Bearer-key requests don't need it.

### Conventions

- All IDs are UUIDv7 strings.
- All times are ISO 8601 UTC.
- Errors return `{ "error": { "code": "...", "message": "..." } }`. Some errors add fields inside `error`, e.g. `missing` for `missing_blobs` and `head_version_id` for `head_moved`.
- Body validation errors return `422` with code `validation_error`.

### Time and clocks

Machines may be in different time zones and their clocks may drift. To keep comparisons correct:

- **Every time a client sends must carry a UTC offset** (`2026-11-20T18:00:00-04:00` or `…Z`). The server converts everything to UTC. A time without an offset is rejected with `422`, so a machine's local time is never mistaken for UTC.
- Clients should send their current clock as `X-Savepoint-Client-Time: <ISO 8601 with offset>` on requests. The server records each machine's clock offset (`clock_skew_ms` = client minus server) and shows it in the web UI.
- `GET /machines/me` returns `server_time`, so a client can measure its own offset directly and correct for it.
- When the client compares `L` and `H` in a conflict (§6), it compares UTC times: the latest file mtime of `L` against `H`'s `latest_mtime`.

### Machines

| Method | Path | Notes |
|---|---|---|
| `GET` | `/machines/me` | Machine key only. Returns `{id, name, server_time, clock_skew_ms}`. Also updates `last_seen`. |
| `GET` | `/machines` | UI only. Returns `{machines: [{id, name, created_at, last_seen_at, revoked_at, clock_skew_ms, clock_checked_at}]}`. |
| `POST` | `/machines` | UI only. Body: `{name}`. Returns `{id, name, api_key}`. The key is shown once and stored as a SHA-256 hash. |
| `DELETE` | `/machines/{id}` | UI only. Revokes the key. |

### Games

| Method | Path | Notes |
|---|---|---|
| `GET` | `/games` | Returns `{games: [...]}`, excluding deleted games. Each game includes its roots with any path override for the calling machine applied (`resolved_template`). |
| `POST` | `/games` | Create a game. |
| `GET` | `/games/{id}` | |
| `PATCH` | `/games/{id}` | |
| `DELETE` | `/games/{id}` | Soft delete. Versions are kept until purged from the UI. |
| `PUT` | `/games/{id}/roots/{alias}/override` | Body: `{path_template}`. Sets the path override for the calling machine. A null `path_template` clears it. With a session cookie, pass `?machine_id=` to choose the machine. |

Game object:

```json
{
  "id": "…",
  "name": "Elden Ring",
  "roots": [
    { "alias": "saves", "path_template": "%APPDATA%\\EldenRing", "resolved_template": "%APPDATA%\\EldenRing" }
  ],
  "exe_names": ["eldenring.exe"],
  "excludes": ["**/*.tmp", "**/shader_cache/**"],
  "sync_mode": "auto",
  "retention": null,
  "head_version_id": "…",
  "created_at": "…",
  "updated_at": "…",
  "deleted_at": null
}
```

- `sync_mode` is `"auto"` or `"manual"`.
- `retention: null` means the game uses the global default (see §7). Otherwise it is a full retention object; omitted fields take their defaults.
- Root aliases match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and must be unique per game, ignoring case.
- `PATCH` with `roots` replaces the whole list. Overrides for removed aliases are deleted.

### Heads (polling)

| Method | Path | Notes |
|---|---|---|
| `GET` | `/heads` | Returns `{ "<game_id>": {"version_id", "created_at", "machine_id"}, … }`. Clients poll this every 60 s. Supports `ETag` / `If-None-Match`, so an unchanged list costs a 304. |

### Blobs

| Method | Path | Notes |
|---|---|---|
| `POST` | `/blobs/check` | Body: `{hashes: [...]}`. Returns `{missing: [...]}`. |
| `PUT` | `/blobs/{sha256}` | Raw body. The server verifies the hash before storing (`400 hash_mismatch` otherwise). Idempotent: `201` when stored, `200` when it already existed. |
| `GET` | `/blobs/{sha256}` | Raw content. |

### Versions

| Method | Path | Notes |
|---|---|---|
| `GET` | `/games/{id}/versions` | Query: `limit` (default 50, max 500), `before` (a version id), `type`, `tag`. Newest first. Returns `{versions: [...], next_before}`; pass `next_before` as `before` for the next page (null on the last page). |
| `POST` | `/games/{id}/versions` | Creates a version (see below). |
| `GET` | `/versions/{id}` | Metadata plus manifest. |
| `GET` | `/versions/{id}/diff?against={id}` | Lists added, changed and removed paths. Without `against`, compares with the previous version. |
| `GET` | `/versions/{id}/download` | The whole version as a zip. |
| `PATCH` | `/versions/{id}` | Body: `{tags?, note?, pinned?}`. |
| `POST` | `/versions/{id}/promote` | Creates a `promoted` version copying this manifest and makes it head (`201`). Clients then pull it like any other new head. Promoting the current head returns it with `"unchanged": true` (`200`). |
| `DELETE` | `/versions/{id}` | UI only. Not allowed on the current head. |

`POST /games/{id}/versions` body:

```json
{
  "parent_version_id": "…",
  "type": "session",
  "make_head": true,
  "client_created_at": "…",
  "files": [{ "path": "saves/ER0000.sl2", "hash": "…", "size": 28967888, "mtime": "…" }]
}
```

- `parent_version_id` is the version the client last synced. It is null for the first version.
- `type` is one of `session`, `checkpoint`, `manual`, `offline`, `pre_restore`. Only `/promote` creates `promoted`.
- `mtime` and `client_created_at` must include a UTC offset (see "Time and clocks").
- Paths are `<root alias>/<relative path>` with forward slashes. The alias must be one of the game's roots. Empty, `.` and `..` segments are rejected, as are paths that differ only by case.
- Returns `201` with the new version, or:
  - `200` with the existing head and `"unchanged": true` if the manifest has the same paths and hashes as head's (mtimes alone don't count as a change). This is checked first.
  - `400 missing_blobs` with `error.missing: [hashes]` if any referenced blob hasn't been uploaded.
  - `400 size_mismatch` if a file's `size` differs from its blob's size.
  - `400 invalid_path`, `unknown_root` or `duplicate_path` for bad paths.
  - `409 head_moved` with `error.head_version_id` if `make_head` is true but `parent_version_id` is not the current head. Another machine pushed first, so this is a conflict. Upload again with `make_head: false`, then resolve it (§6).

Version object:

```json
{
  "id": "…",
  "game_id": "…",
  "parent_version_id": "…",
  "source_version_id": null,
  "type": "session",
  "machine_id": "…",
  "machine_name": "Gaming PC",
  "created_at": "…",
  "client_created_at": "…",
  "file_count": 3,
  "total_size": 28968912,
  "latest_mtime": "…",
  "tags": ["before-malenia"],
  "note": null,
  "pinned": false,
  "is_head": true
}
```

- `latest_mtime` is the newest file mtime in the manifest, in UTC. It is what "newer" means in a conflict.
- `source_version_id` is set on `promoted` versions: it is the version that was copied.
- `machine_id` is null for versions created from the web UI.
- `GET /versions/{id}` adds `files: [{path, hash, size, mtime}]`.
- `POST` responses add `unchanged`.

### Misc

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | No auth. Returns `{status, server_time}`. |

---

## 5. Server (`savepoint-server`)

### Stack

- Python 3.12, FastAPI, SQLAlchemy 2 + SQLite (WAL mode), Alembic
- argon2-cffi for the admin password
- zstandard for blob compression
- APScheduler for nightly jobs

### Web UI

Server-rendered Jinja2 templates with htmx, so there is no frontend build step. It must work well on mobile.

### Data model

```
games(id, name, exe_names_json, excludes_json, sync_mode, retention_json, head_version_id,
      created_at, updated_at, deleted_at)
game_roots(game_id, alias, path_template)
root_overrides(game_id, alias, machine_id, path_template)
machines(id, name, api_key_hash, created_at, last_seen_at, revoked_at,
         clock_skew_ms, clock_checked_at)
versions(id, game_id, parent_version_id, source_version_id, type, machine_id, created_at,
         client_created_at, file_count, total_size, latest_mtime, note, pinned)
version_files(version_id, path, blob_hash, size, mtime)
version_tags(version_id, tag)
blobs(hash, size, stored_size, created_at)
settings(key, value_json)           -- default retention, offsite config, etc.
admin(id=1, password_hash, session_epoch)   -- epoch bump signs out other sessions
job_runs(id, job, started_at, finished_at, status, summary, log)
```

### Blob storage

- Blobs are stored at `/data/blobs/<hash[0:2]>/<hash[2:4]>/<hash>.zst`.
- Writes go to a temp file, then fsync, then an atomic rename.
- The hash is always of the uncompressed content.

### Admin password

- On first start, the admin password is taken from the `SAVEPOINT_ADMIN_PASSWORD` env var and stored hashed.
- After that it can be changed in the UI. Changing it signs out every other session.
- To recover a forgotten password, set `SAVEPOINT_RESET_ADMIN_PASSWORD=true` with a new `SAVEPOINT_ADMIN_PASSWORD` and restart.
- Login is rate-limited per client IP (5 failures per 15 minutes).

### Jobs

- **Retention + GC (nightly)**
  1. Apply retention rules (§7).
  2. Delete blobs that no version references and that are older than a grace period (default 24 h, `SAVEPOINT_GC_GRACE_HOURS`). The grace period stops GC from deleting blobs a client uploaded seconds before creating its version.
  3. Log what was removed in `job_runs`, viewable in the UI.
- **Offsite backup (nightly, optional)**
  1. Run SQLite `.backup` to `/data/backup/savepoint.db`.
  2. Run `rclone sync /data/blobs` and the DB copy to the configured remote (e.g. `gdrive:savepoint`).
  3. The rclone config is mounted read-only.
  4. The job is enabled in UI settings, which also show the last run's status.

### Web UI pages

1. **Login**
2. **Games**
   - Lists each game's name, head time, which machine made it, mode, total stored size, and last activity.
   - Lets you add a game.
3. **Game detail**
   - A version timeline showing type, machine, time, size, tags, note and pin.
   - Filters by type and tag.
   - Per-version actions: diff, download zip, tag, note, pin, promote to head, delete.
4. **Game settings**
   - Roots and per-machine overrides
   - Exe names
   - Excludes
   - Sync mode
   - Retention override
5. **Machines**
   - Create a machine (shows its key once)
   - Revoke a key
   - Last seen
6. **Settings**
   - Default retention
   - Offsite backup toggle and remote name, with last-run status
   - Change password
   - Storage stats

### Deployment

- The app lives in `~/apps/savepoint/` and has its own `docker-compose.yml`.
- **Compose settings:**
  - external network `media`
  - `PUID`/`PGID` 1000
  - `TZ=America/Port_of_Spain`
  - volume `./data:/data`
- **Deploy:** follows the usual workflow (git push → SSH pull → `deploy.sh`).
- **Nginx Proxy Manager:**
  - proxy host `savepoint.8hzwanip.com`, with SSL forced
  - add `client_max_body_size 0;` in the Advanced tab, or uploads larger than 1 MB will fail
  - long `proxy_read_timeout` for large uploads
- **Watchtower:** don't auto-update. The image is built locally.

---

## 6. Windows client (`savepoint`)

### Stack

- .NET 8, C#
- WinForms tray app (NotifyIcon plus a few dialogs)
- Microsoft.Data.Sqlite for local state
- Windows App SDK / CommunityToolkit for toast notifications

### Solution layout

```
savepoint/
├── src/
│   ├── Savepoint.Core/        # API client, manifest scanning, hashing, sync engine, state store
│   ├── Savepoint.Agent/       # tray app, watcher, exe monitor, IPC server, UI dialogs
│   └── Savepoint.Playnite/    # phase 6; thin plugin that talks to Agent over IPC
├── tests/
│   └── Savepoint.Core.Tests/
├── docs/DESIGN.md
└── .github/workflows/release.yml
```

### Local state

Local state is stored in `%LOCALAPPDATA%\Savepoint\state.db`, with these tables:

- `config` (server URL, API key encrypted with DPAPI, machine id/name, start with Windows)
- `game_state(game_id, last_synced_version_id, last_synced_manifest_json, last_scan_at)`
- `pending_ops(id, game_id, op, payload_json, created_at, attempts)` holds queued uploads for when the server is unreachable
- `local_snapshots(id, game_id, path, created_at)`

The Agent also keeps the last 3 pre-restore snapshots per game as zips in `%LOCALAPPDATA%\Savepoint\snapshots\`. This works even when offline, as a second safety net.

### Scanning

- The Agent walks each resolved root, applying excludes (glob).
- For each file it records the size and mtime. It only re-hashes a file if those differ from the cached manifest entry.
- Files open with exclusive locks are retried with backoff. If a file is still locked, the scan is aborted and retried later rather than uploading a partial manifest.

### Triggers

| Trigger | Action |
|---|---|
| Agent startup | Pull check for all games |
| `/heads` poll shows a changed head | Pull check for that game |
| Game exe exits | Push, type `session` |
| File watcher quiet for N min (default 3, configurable) | Push, type `checkpoint` |
| Periodic rescan every 15 min | Push, type `checkpoint`. Catches events the watcher missed, since FileSystemWatcher can overflow. |
| Tray "Back up now" | Push, type `manual` |
| Tray "Restore latest" | Pull, forced |
| Playnite pre-launch | Pull, blocking with a timeout (default 30 s) |
| Playnite post-exit | Push, type `session` |

### Exe monitor

- Polls the process list every 5 s and matches each game's `exe_names` case-insensitively.
- This needs no admin rights, unlike WMI process-trace events.

### Sync algorithm

Definitions:

- `L` = local manifest (fresh scan)
- `S` = last-synced manifest/version
- `H` = server head

**Pull check:**

1. If the game's exe is running, skip and retry after it exits.
2. Scan to get `L`, and fetch `H`.
3. If `L` = `S` and `H` = `S`, do nothing.
4. If `L` = `S` and `H` ≠ `S`, the server has moved on and there are no local changes:
   - **auto mode:** restore `H`.
   - **manual mode:** show a toast offering to restore.
5. If `L` ≠ `S` and `H` = `S`, there are local changes the server doesn't have (e.g. you played offline). Push `L` as type `offline`.
6. If `L` ≠ `S` and `H` ≠ `S`, it's a **conflict**:
   1. Upload `L` with `make_head: false`, so it's preserved.
   2. **auto mode:** the newer side wins, judged by the latest file mtime in each manifest, compared in UTC (`H`'s `latest_mtime` against the newest mtime in `L`, minus this machine's measured clock offset). If `L` is newer, promote the uploaded local version to head. If `H` is newer, restore `H`.
   3. **manual mode:** show a toast offering "keep local / take server / decide in web UI". Do nothing until the user picks.

**Push (session / checkpoint / manual):**

1. Scan to get `L`. If `L` = `S`, do nothing.
2. Fetch `H`. If `H` ≠ `S`, handle it as a conflict (step 6 above).
3. Upload:
   1. `POST /blobs/check`
   2. `PUT` the missing blobs, in parallel with a limit of 4
   3. `POST /versions` with `make_head: true`. If it returns `409 head_moved`, another machine pushed first: handle it as a conflict (step 6 above).
4. Set `S` = the new version.
5. If the server is unreachable, add the push to `pending_ops` and retry with backoff.

**Restore `V`:**

1. Take a local zip snapshot. If `L` ≠ `S`, also upload `L` as `pre_restore`.
2. Download the missing blobs to a staging dir under `%LOCALAPPDATA%\Savepoint\staging\` and verify their hashes.
3. Mirror into the roots:
   - write changed or new files (write to a temp file, then rename)
   - delete files that are not in `V` and not excluded
   - set each file's mtime from the manifest
4. Set `S` = `V`.

### Tray UI

- **Tray menu:**
  - status summary
  - per-game submenu: Back up now, Restore latest, Open in web UI
  - Add game…
  - Settings
  - Pause sync
  - Quit
- **Add/Edit game dialog:**
  - name
  - roots, each with an alias and a Browse… button that opens common save locations (AppData, Documents, the emulator folders above)
  - exe names, picked from running processes or by browsing for the .exe
  - excludes
  - mode
  - "this machine only" path override
- **Settings:**
  - server URL
  - API key
  - start with Windows
  - watcher quiet period
  - poll interval
- **Toasts:**
  - restore done
  - conflict needs a decision
  - server unreachable for more than 1 hour
  - pending queue flushed
- **Tray icon states:**
  - idle
  - syncing
  - offline
  - attention needed

### IPC (for Playnite and scripts)

The Agent serves a named pipe, `\\.\pipe\savepoint`, using JSON lines. Commands:

- `pull {game_id}` blocks until the pull is done
- `push {game_id, type}`
- `status {game_id?}`
- `list_games`

### Playnite plugin (phase 6)

- A GenericPlugin that links a Playnite game to a Savepoint game. The mapping is stored in the plugin settings, chosen from a dropdown fed by `list_games`.
- Before a game starts, it sends `pull` over the pipe and waits. If the Agent isn't running, it shows a warning and lets the game launch anyway.
- After the game stops, it sends `push … session`.

### Release

- A GitHub Action (on a `v*` tag, running on `windows-latest`) runs `dotnet publish -c Release -r win-x64 --self-contained -p:PublishSingleFile=true`.
- It attaches `Savepoint.exe` to a GitHub Release.

---

## 7. Retention

Retention is set as a global default, with optional per-game overrides:

```json
{
  "keep_newest": 5,
  "checkpoint_keep_hours": 48,
  "session_keep_all_days": 14,
  "session_daily_days": 90,
  "session_weekly_days": 365,
  "max_versions": null
}
```

The nightly job applies these rules:

- **Never deleted:**
  - the current head
  - pinned versions
  - tagged versions
  - the newest `keep_newest` versions regardless of age
- **Checkpoints, offline, pre-restore:** deleted after `checkpoint_keep_hours`.
- **Session, manual, promoted:**
  - all kept for `session_keep_all_days`
  - then the newest per day until `session_daily_days`
  - then the newest per week until `session_weekly_days`
  - then deleted
- **`max_versions`:** if set, this is a hard cap applied last, after the protected versions.
- Every value is configurable, and `null` means no limit for that rule. For example, `checkpoint_keep_hours: null` keeps checkpoints forever, and `session_weekly_days: null` keeps one session per week forever.
- "Per day" and "per week" (ISO weeks) use the server's `TZ`, so a late-night session counts toward the local day it was played on.

---

## 8. Build phases

| # | Phase | Repo | Done when |
|---|---|---|---|
| 1 | Server core: models, blob store, API, auth, machine keys | server | API passes integration tests; deployed behind NPM |
| 2 | Client core: settings, add/edit game, scan, manual push and pull | client | A game can be backed up on the PC and restored on the laptop by hand |
| 3 | Automation: exe monitor, watcher, head polling, auto-restore, conflicts, offline queue | client | Play on the PC, close the game, open the laptop: the save is already there |
| 4 | Web UI: games, timeline, diff, tags, pins, promote, download, machines, settings | server | Roll back from a phone and see the PC pick it up |
| 5 | Retention + GC + offsite rclone | server | Nightly job runs; Drive copy restorable |
| 6 | Playnite plugin | client | Launching from Playnite always pulls first |

---

## 9. Testing

- **Server:**
  - pytest integration tests on a temp data dir, covering the blob-check/upload/version flow, missing-blob rejection, unchanged-manifest dedupe, promote, retention rules (with fixed clocks) and GC
- **Client:**
  - unit tests for path template resolution, exclude globbing and manifest diffing
  - the sync decision table (§6), tested with all six L/S/H combinations in both modes
  - restore mirroring against a temp folder, including deletions and excluded files left untouched
- **End to end:**
  - run the server in Docker plus two Agent instances pointed at different temp roots, simulating the two machines
