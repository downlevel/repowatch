# Self-hosting RepoWatch

RepoWatch runs for one GitHub account per installation. There is no signup service,
central login or dependency on TeleLogin. Your GitHub tokens and database stay on your server.

## Start with Docker Compose

Install Docker Engine with the Compose plugin, then:

```bash
git clone https://github.com/downlevel/repowatch.git
cd repowatch
cp .env.example .env
```

Edit `.env` and set these values before starting:

| Setting | Value |
| --- | --- |
| `GITHUB_OWNER` | The GitHub username or organisation to monitor |
| `GITHUB_TOKEN` | Your GitHub token; see the permissions in the README |
| `DASHBOARD_AUTH` | `basic` for the built-in login (default) |
| `DASHBOARD_USER` | Your dashboard username |
| `DASHBOARD_PASSWORD` | A long, unique dashboard password |

A GitHub token is not your dashboard password. Never commit `.env`. For passwords
containing `$` or `#`, put the value in single quotes in `.env` so Compose treats it literally.
A random password can be generated with `openssl rand -hex 24`.

```bash
docker compose up -d --build
docker compose ps
```

Open **http://127.0.0.1:8765**. The browser asks for your dashboard credentials.
Compose publishes the port on loopback by default, not on every server interface.
If port 8765 is already occupied, set `REPOWATCH_PORT` to another available port in `.env`.

Two services start:

- `web` serves the dashboard, with an authenticated health check. It does not receive your GitHub token.
- `collector` collects immediately after the dashboard is healthy, then waits six hours
  between completed runs. Set `COLLECT_INTERVAL_SECONDS` to change this (minimum 60 seconds).
  No host cron setup is required. A failed run is logged and the next scheduled attempt still runs.

Both use the `repowatch-data` named volume for SQLite. Traffic history starts with GitHub's
available 14-day window; counter-based trends need subsequent observations. Data may take
a little while to appear during the first collection.

## Choose authentication explicitly

### Built-in login (recommended default)

```dotenv
DASHBOARD_AUTH=basic
DASHBOARD_USER=admin
DASHBOARD_PASSWORD='<your-own-long-password>'
```

Missing either credential prevents the web service from starting. Basic authentication
protects the static dashboard and all API paths, including write endpoints. There are no
user accounts, registration emails or password-reset service to manage. Change the password
in `.env` and recreate the web container to rotate it:

```bash
docker compose up -d web
```

Use HTTPS when accessing the dashboard over a network; HTTP Basic does not encrypt credentials.
For local testing, use the loopback address above. For a remote server, an SSH tunnel is
another option: `ssh -L 8765:127.0.0.1:8765 your-server`, then open the local address.

### Existing authentication proxy

If you already use TeleLogin, Authelia, Authentik or another authenticating reverse proxy:

```dotenv
DASHBOARD_AUTH=external
```

This explicitly disables authentication **inside RepoWatch**. Configure your proxy to protect
all paths, including `/api/`, `/app.js` and `/style.css`. Keep direct access to the backend
restricted to the proxy. RepoWatch does not create a second login or interpret proxy identity headers.

A proxy installed on the host can reach `127.0.0.1:8765`. A proxy running in Docker should
share a Docker network with `web` and proxy to its container port `8765`; configure that
network in a local Compose override. Do not expose an unauthenticated backend to the Internet.
The example in `deploy/nginx.conf.example` also works under a URL prefix such as `/repowatch/`.

For an existing manual installation behind a login proxy, add `DASHBOARD_AUTH=external`
to its `.env` before upgrading. Previously, omitting both credentials implicitly disabled login;
this release requires an explicit mode.

## Operations

Inspect collection progress and errors:

```bash
docker compose logs --tail=100 web collector
```

Perform an extra collection after stopping the periodic collector to avoid overlapping runs:

```bash
docker compose stop collector
docker compose run --rm collect
docker compose start collector
```

Create a verified, compressed database backup:

```bash
docker compose run --rm backup
docker compose run --rm backup backup --list
```

The second command overrides the tool's default command with `backup --list`.
Copies live in `/data/backups` in the persistent volume. Copy an actual filename from
that list to your machine:

```bash
docker compose run --rm --no-deps -T --entrypoint cat backup /data/backups/repowatch-<timestamp>.db.gz > backup.db.gz
```

Store a copy outside this Docker volume. The `backup` tool is manual; schedule the command
on your server if you want automatic copies. Existing systemd and optional R2 examples
are in `deploy/` for manual installations.

To restore a copy already in `/data/backups`, replace the filename below with the actual
backup filename. Stop both services first. The restore verifies the decompressed copy before
using SQLite's backup API to replace the database, including its WAL state:

```bash
docker compose stop web collector
docker compose run --rm --no-deps -T --entrypoint python backup -c '
import gzip, shutil, sqlite3, tempfile
from pathlib import Path
from contextlib import closing
source = Path("/data/backups/repowatch-<timestamp>.db.gz")
with tempfile.TemporaryDirectory(dir="/data") as work:
    raw = Path(work) / "restore.db"
    with gzip.open(source, "rb") as reader, raw.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    with closing(sqlite3.connect(f"file:{raw}?mode=ro", uri=True)) as src:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
        if src.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0:
            raise RuntimeError("Backup contains no repositories")
        with closing(sqlite3.connect("/data/repowatch.db")) as dst:
            src.backup(dst)
print("Database restored")
'
docker compose start web collector
```

To restore an exported copy, first copy it back into `/data/backups` using a temporary
container with the same volume. Keep a backup of the current database before restoring.

Update to a new version:

```bash
docker compose run --rm backup
git pull --ff-only
docker compose up -d --build
```

`docker compose down` stops the services and keeps the data volume. `down -v` deletes the
volume and its history. Neither your database nor `.env` is included in the Docker image.
The containers run as UID/GID 10001 with a read-only root filesystem.

## Troubleshooting

- **Missing GitHub owner/token:** Compose stops with the variable name to configure.
- **Missing dashboard credentials or an invalid login mode:** `web` exits with a configuration
  error and the dependent collector does not start. Check `docker compose logs web`.
- **Login prompt repeats:** verify both credentials and whether your proxy forwards the
  `Authorization` header when using built-in Basic authentication.
- **Collector reports an API failure:** check token permissions, repository access, token
  expiry and the logs. A healthy web service does not imply collection succeeded.
- **Old or incomplete data:** inspect the last collection status and coverage in the dashboard.
- **Switching to a host bind mount:** give its data directory to UID/GID 10001 first; the
  supplied named volume handles initial ownership automatically.

## Repeatable installation check

Developers can test the installation with synthetic credentials and a fake GitHub API:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/smoke-compose.py --build
```

This creates a uniquely named Compose project and volume, checks authentication, collection,
persistence and a verified backup, then removes only its own resources. It never loads the
real `.env` or production data. A Docker daemon and permission to use it are required.
