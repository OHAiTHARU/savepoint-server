# savepoint-server

The server half of Savepoint: self-hosted game save sync and versioned backup.
It stores every save version, serves the `/api/v1` sync API for the Windows
tray app, and has a mobile-friendly web UI for browsing, diffing, rolling back
and downloading versions.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full spec and the API contract.

## What's here

| Area | Where |
|---|---|
| API (`/api/v1`): machines, games, heads, blobs, versions | `app/api/` |
| Version logic: create, dedupe, conflict check, promote, diff, zip | `app/services/versions.py` |
| Retention rules and blob GC | `app/services/retention.py` |
| Offsite backup (SQLite backup + rclone) | `app/services/offsite.py` |
| Nightly scheduler and job history | `app/jobs.py` |
| Web UI (Jinja2 + htmx, no build step) | `app/web/` |
| Blob store (zstd, content-addressed, atomic writes) | `app/blobstore.py` |
| Database migrations (Alembic, run on startup) | `migrations/` |

Interactive API docs are served at `/api/docs`.

## Deploy (homelab)

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/OHAiTHARU/savepoint-server savepoint
cd savepoint
cp .env.example .env   # set SAVEPOINT_ADMIN_PASSWORD
./deploy.sh
```

`deploy.sh` builds the image, starts the container and waits for its health
check. The compose file:

- joins the external `media` network
- runs as `PUID`/`PGID` 1000
- sets `TZ=America/Port_of_Spain`, used for nightly job times and day/week retention buckets
- stores everything in `./data` (database, blobs, backups, session secret)
- mounts `./rclone` read-only for the offsite backup
- opts out of Watchtower, since the image is built locally

### Nginx Proxy Manager

- Proxy host `savepoint.8hzwanip.com` → `savepoint:8080`, with SSL forced.
- In the **Advanced** tab:

  ```nginx
  client_max_body_size 0;
  proxy_request_buffering off;
  proxy_read_timeout 3600s;
  proxy_send_timeout 3600s;
  ```

  Without `client_max_body_size 0`, uploads larger than 1 MB fail.

Session cookies are HTTPS-only. To try the UI over plain HTTP without the
proxy, set `SAVEPOINT_SECURE_COOKIES=false`.

### First steps after deploy

1. Sign in with `SAVEPOINT_ADMIN_PASSWORD`, then change it under **Settings**.
2. Under **Machines**, create a key for each PC. Each key is shown once.
3. Add games here or from the tray app.

### Offsite backup to Google Drive

1. On any machine with a browser, run `rclone config` and create a remote named `gdrive`.
2. Copy the resulting `rclone.conf` to `~/apps/savepoint/rclone/rclone.conf`.
3. In **Settings → Offsite backup**, enable it, set the remote (e.g. `gdrive:savepoint`) and press **Run now** to test.

Each nightly run writes a consistent copy of the database to
`data/backup/savepoint.db`, then syncs `data/blobs` and that copy to the
remote. To restore, copy both back into `data/` (as `data/blobs/` and
`data/savepoint.db`) and start the container.

### Configuration

| Variable | Default | |
|---|---|---|
| `SAVEPOINT_ADMIN_PASSWORD` | — | Initial admin password (first start only) |
| `SAVEPOINT_RESET_ADMIN_PASSWORD` | `false` | Overwrite the stored password from the env var on start |
| `SAVEPOINT_DATA_DIR` | `/data` | |
| `SAVEPOINT_SECRET_KEY` | generated | Signs session cookies; stored in `data/secret_key` if unset |
| `SAVEPOINT_SECURE_COOKIES` | `true` | |
| `SAVEPOINT_SESSION_DAYS` | `30` | Web session lifetime |
| `SAVEPOINT_RETENTION_TIME` | `03:30` | Nightly retention + GC, in `TZ` |
| `SAVEPOINT_OFFSITE_TIME` | `04:15` | Nightly offsite backup, in `TZ` |
| `SAVEPOINT_GC_GRACE_HOURS` | `24` | Unreferenced blobs younger than this are kept |
| `SAVEPOINT_ENABLE_SCHEDULER` | `true` | |
| `RCLONE_CONFIG` | — | Path to `rclone.conf` inside the container |
| `TZ` | `UTC` | |

Retention values live in the database and are edited in the UI (global
default, plus optional per-game overrides).

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

To run locally:

```bash
SAVEPOINT_DATA_DIR=./dev-data SAVEPOINT_ADMIN_PASSWORD=devpassword SAVEPOINT_SECURE_COOKIES=false uvicorn app.asgi:app --reload --port 8080
```

After changing `app/models.py`, add a migration:

```bash
alembic revision --autogenerate -m "describe the change"
```
