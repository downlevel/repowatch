# Manual installation (Linux, Python 3.11+)

This guide is for Python installations without Docker. Docker users should follow
[self-hosting](self-hosting.md), which already schedules collection automatically.

## Install and configure

```bash
git clone https://github.com/downlevel/repowatch.git
cd repowatch
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Edit `.env`: configure `GITHUB_OWNER`, `GITHUB_TOKEN`, `DASHBOARD_USER` and
`DASHBOARD_PASSWORD`. See the [token guide](../README.md#github-access).
Choose `DASHBOARD_AUTH=basic` (default) or explicitly set `external` if your reverse proxy
already authenticates all dashboard/API requests. Both credentials are required in Basic mode.

## First collection and dashboard

```bash
.venv/bin/repowatch collect
.venv/bin/repowatch serve
```

Open http://127.0.0.1:8765 and use the dashboard credentials. Use an HTTPS reverse proxy
or SSH tunnel for remote access. The collector exits with `0` for success, `2` for partial
section failures and `1` for a fatal error; check logs before assuming all sources are complete.
Request counts depend on the number of repositories, pagination, stars, activity and CI jobs.

## Schedule collection

Choose either a collection loop supervised by your process manager:

```bash
.venv/bin/repowatch collect-loop
```

Or configure cron, replacing `/opt/repowatch` with your actual installation path:

```cron
0 6 * * * cd /opt/repowatch && .venv/bin/repowatch collect >> /var/log/repowatch.log 2>&1
```

Do not run both against the same database. Ensure the cron user can write to the log path
and database directory. `COLLECT_INTERVAL_SECONDS` configures the loop (default six hours,
minimum 60 seconds); it does not alter your cron schedule. See the service examples in `deploy/`.

## Backups and restore

Create a consistent, verified copy before upgrades:

```bash
.venv/bin/repowatch backup --apply
.venv/bin/repowatch backup --list
```

Rotation retains the configured daily/weekly/monthly copies and only deletes with `--apply`.
Systemd backup and optional R2 upload examples are in `deploy/`; installation of these timers
is separate from running the dashboard. Keep a copy outside the server.

**Before restoring, stop `repowatch serve`, stop any `collect-loop` process and pause cron or
timers that could collect during restore.** Preserve a backup of the current database too.
Replace the example filename with a real verified backup. This command reads the target
path from your `.env`, verifies the decompressed copy and restores through SQLite's backup API:

```bash
BACKUP_PATH='data/backups/repowatch-<timestamp>.db.gz' .venv/bin/python - <<'PY'
import gzip
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from repowatch.config import Config

cfg = Config.from_env()
source = Path(os.environ["BACKUP_PATH"])
with tempfile.TemporaryDirectory(dir=cfg.db_path.parent) as work:
    raw = Path(work) / "restore.db"
    with gzip.open(source, "rb") as reader, raw.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    with closing(sqlite3.connect(f"{raw.resolve().as_uri()}?mode=ro", uri=True)) as src:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
        if src.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0:
            raise RuntimeError("Backup contains no repositories")
        with closing(sqlite3.connect(cfg.db_path)) as dst:
            src.backup(dst)
print("Database restored")
PY
```

Resume the dashboard and your chosen collection scheduler after a successful restore.
Do not decompress a backup directly over an active SQLite database: its WAL state may still
contain transactions from the previous database.

## Upgrade

Back up the database, stop web/collection processes, then:

```bash
git pull --ff-only
.venv/bin/pip install -e .
```

For installations that previously omitted dashboard credentials behind an authentication
proxy, set `DASHBOARD_AUTH=external` before restarting. Restart your web and collection
processes, check logs and verify the dashboard. Local `.env` and database files are ignored by Git.
