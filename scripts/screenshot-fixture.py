#!/usr/bin/env python3
"""Serve synthetic documentation data on loopback; never load the real .env/database."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repowatch import db


def seed_demo(path: Path, now: datetime) -> None:
    if path.exists():
        raise FileExistsError("Refusing to overwrite an existing database")
    c = db.connect(path)
    try:
        projects = [
            ("launchpad", "A small release toolkit for independent developers"),
            ("repo-lens", "A lightweight dashboard for your open-source projects"),
            ("tiny-cli", "A new command-line project with a short history"),
            ("archived-kit", "A quiet project, kept for reference"),
        ]
        for i, (name, description) in enumerate(projects):
            c.execute(
                "INSERT INTO repos (name,description,html_url,archived,first_seen,last_seen) VALUES (?,?,?,?,?,?)",
                (name, description, f"https://github.com/example/{name}", int(i == 3),
                 (now - timedelta(days=40)).isoformat(), now.isoformat()),
            )
            stars, downloads = 20 + i * 7, 1000 + i * 400
            for back in reversed(range(41)):
                if (i == 2 and back > 4) or (i == 1 and back == 6):
                    continue
                day = (now - timedelta(days=back)).date().isoformat()
                visitors = 0 if i == 3 else 5 + (back * 7) % 18 + (120 if back == 7 and i == 0 else 0)
                stars += 0 if i == 3 else (3 if back in (7, 6, 5) and i == 0 else int(back % 3 == 0))
                downloads += visitors * 2
                c.execute("INSERT INTO traffic_daily VALUES (?,?,?,?,?,?)", (name, day, visitors * 2, visitors, 8, 3))
                c.execute("INSERT INTO repo_snapshots VALUES (?,?,?,2,1,0)", (name, day, stars))
                if i in (0, 1, 3):
                    c.execute("INSERT INTO release_asset_snapshots VALUES (?,?,?,?,?)", (name, day, "v1.0", "app.tar.gz", downloads))
                if i == 1:
                    c.execute("INSERT INTO pypi_daily VALUES (?,?,?,?)", (name, name, day, visitors * 3))
            if i < 2:
                c.execute("INSERT INTO releases VALUES (?, 'v1.0', 'First release', ?, 0, ?)",
                          (name, (now - timedelta(days=40)).isoformat(), f"https://github.com/example/{name}/releases"))
                for site, count in (("reddit.com", 256 - i * 50), ("github.com", 120),
                                    ("news.ycombinator.com", 65), ("google.com", 32)):
                    c.execute("INSERT INTO referrers_snapshots VALUES (?,?,?,?,?)", (name, now.date().isoformat(), site, count, count // 2))
        for i in range(12):
            repo = "launchpad" if i % 2 == 0 else "repo-lens"
            kind = ("issue", "pr", "comment")[i % 3]
            c.execute(
                "INSERT INTO activity (repo,kind,gh_id,number,user,is_owner,is_bot,title,url,created_at) VALUES (?,?,?,?,?,0,0,?,?,?)",
                (repo, kind, 1000 + i, i + 1, "sample-contributor", "Example community activity",
                 f"https://github.com/example/{repo}/issues/{i + 1}", (now - timedelta(days=1 + i % 10)).isoformat()),
            )
        examples = [("launchpad", "star", "3 new stars on launchpad"),
                    ("repo-lens", "issue", "First community issue on repo-lens"),
                    ("launchpad", "download", "42 release downloads on launchpad")]
        for i, (repo, kind, text) in enumerate(examples):
            db.add_event(c, str(i), repo, now.isoformat(), kind, text)
        c.execute("INSERT INTO collector_runs (started_at,finished_at,repos,errors) VALUES (?,?,4,0)",
                  (now.isoformat(), now.isoformat()))
        c.commit()
    finally:
        c.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="A NEW temporary database path")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    seed_demo(args.db, datetime.now(timezone.utc))
    import uvicorn
    from repowatch.config import Config
    from repowatch.web import create_app
    app = create_app(Config(token="", owner="demo-account", db_path=args.db, dashboard_auth="external"))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
