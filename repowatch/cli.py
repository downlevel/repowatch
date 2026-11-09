"""Command line entry point: `repowatch collect` and `repowatch serve`."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys

from . import backup, db
from .collector import Collector
from .config import Config
from .github import GitHub, GitHubError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repowatch", description=__doc__)
    parser.add_argument("--env-file", help="path to the .env file (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="fetch today's data from GitHub (run daily from cron)")

    serve = sub.add_parser("serve", help="run the dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    backup_cmd = sub.add_parser("backup", help="make a verified copy of the database and rotate old ones")
    backup_cmd.add_argument("--apply", action="store_true", help="really delete; without it rotation is a dry run")
    backup_cmd.add_argument("--rotate-only", action="store_true", help="rotate without making a new copy")
    backup_cmd.add_argument("--list", action="store_true", help="list the backups and exit")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config.from_env(args.env_file)

    if args.command == "collect":
        return collect(cfg)
    if args.command == "backup":
        return run_backup(cfg, apply=args.apply, rotate_only=args.rotate_only, list_only=args.list)
    return serve_dashboard(cfg, args.host, args.port)


def collect(cfg: Config) -> int:
    log = logging.getLogger("repowatch")
    if not cfg.token or not cfg.owner:
        log.error("GITHUB_TOKEN and GITHUB_OWNER must be set (see .env.example)")
        return 1
    conn = db.connect(cfg.db_path)
    gh = GitHub(cfg.token, cfg.api_url)
    stars_gh = GitHub(cfg.stars_token, cfg.api_url) if cfg.stars_token else None
    try:
        errors = Collector(cfg, conn, gh, stars_gh=stars_gh).run()
    except GitHubError as exc:
        log.error("fatal: %s", exc)
        return 1
    finally:
        gh.close()
        if stars_gh:
            stars_gh.close()
        conn.close()
    # Exit code 2 = partial failure, so cron/monitoring can tell it apart from success.
    return 2 if errors else 0


def run_backup(cfg: Config, apply: bool, rotate_only: bool, list_only: bool) -> int:
    if list_only:
        for item in backup.inventory(cfg.backup_dir):
            print(f"{item.name}  {backup.format_size(item.size):>10}")
        return 0

    tiers = backup.Tiers(cfg.backup_keep_daily, cfg.backup_keep_weekly, cfg.backup_keep_monthly)
    try:
        if rotate_only:
            result = backup.rotate(cfg.backup_dir, apply=apply, tiers=tiers)
        else:
            _, result = backup.run(cfg.db_path, cfg.backup_dir, apply=apply, tiers=tiers)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        logging.getLogger("repowatch").error("backup failed: %s", exc)
        return 1

    for item in result.removed:
        print(f"{'removed' if apply else 'would remove'}: {item.name}")
    return 1 if result.errors else 0


def serve_dashboard(cfg: Config, host: str, port: int) -> int:
    import uvicorn

    from .web import create_app

    if host not in ("127.0.0.1", "localhost", "::1") and not (cfg.dashboard_user and cfg.dashboard_password):
        logging.getLogger("repowatch").warning(
            "dashboard exposed on %s without DASHBOARD_USER/DASHBOARD_PASSWORD", host
        )
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
