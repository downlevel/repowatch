"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _pypi_map(value: str | None) -> dict[str, str]:
    """Parse "repo:package,repo2:package2" (or just "repo" when names match)."""
    result: dict[str, str] = {}
    for item in _list(value):
        repo, _, package = item.partition(":")
        result[repo.strip()] = (package or repo).strip()
    return result


@dataclass
class Config:
    token: str
    owner: str
    db_path: Path
    include_forks: bool = False
    include_archived: bool = True
    exclude_repos: list[str] = field(default_factory=list)
    pypi_packages: dict[str, str] = field(default_factory=dict)
    ci_jobs: bool = True
    spike_factor: float = 3.0
    spike_min_uniques: int = 5
    dashboard_user: str | None = None
    dashboard_password: str | None = None
    dashboard_auth: str = "basic"
    api_url: str = "https://api.github.com"
    stars_token: str | None = None
    backup_dir: Path = Path("data/backups")
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4
    backup_keep_monthly: int = 6

    def validate_dashboard_auth(self) -> None:
        if self.dashboard_auth not in {"basic", "external"}:
            raise ValueError("DASHBOARD_AUTH must be 'basic' or 'external'")
        if self.dashboard_auth == "basic":
            if not self.dashboard_user or not self.dashboard_password:
                raise ValueError("DASHBOARD_AUTH=basic requires DASHBOARD_USER and DASHBOARD_PASSWORD")
            if ":" in self.dashboard_user:
                raise ValueError("DASHBOARD_USER cannot contain ':'")

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "Config":
        load_dotenv(env_file or Path.cwd() / ".env")
        return cls(
            token=os.environ.get("GITHUB_TOKEN", ""),
            owner=os.environ.get("GITHUB_OWNER", ""),
            db_path=Path(os.environ.get("REPOWATCH_DB", "data/repowatch.db")),
            include_forks=_bool(os.environ.get("INCLUDE_FORKS"), False),
            include_archived=_bool(os.environ.get("INCLUDE_ARCHIVED"), True),
            exclude_repos=_list(os.environ.get("EXCLUDE_REPOS")),
            pypi_packages=_pypi_map(os.environ.get("PYPI_PACKAGES")),
            ci_jobs=_bool(os.environ.get("CI_JOBS"), True),
            spike_factor=float(os.environ.get("SPIKE_FACTOR", "3")),
            spike_min_uniques=int(os.environ.get("SPIKE_MIN_UNIQUES", "5")),
            dashboard_user=os.environ.get("DASHBOARD_USER") or None,
            dashboard_password=os.environ.get("DASHBOARD_PASSWORD") or None,
            dashboard_auth=os.environ.get("DASHBOARD_AUTH", "basic").strip().lower(),
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            stars_token=os.environ.get("GITHUB_STARS_TOKEN") or None,
            backup_dir=Path(os.environ.get("REPOWATCH_BACKUP_DIR", "data/backups")),
            backup_keep_daily=int(os.environ.get("BACKUP_KEEP_DAILY", "7")),
            backup_keep_weekly=int(os.environ.get("BACKUP_KEEP_WEEKLY", "4")),
            backup_keep_monthly=int(os.environ.get("BACKUP_KEEP_MONTHLY", "6")),
        )
