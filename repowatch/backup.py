"""Verified SQLite backups with tiered rotation.

What can't be rebuilt if the database is lost: GitHub only serves 14 days of traffic, so
everything older exists only here.

The copy goes through SQLite's online backup API, not a file copy: the collector may be
writing, and copying the `.db` without its `-wal` yields a valid file missing the latest
transactions.

Rotation only runs after a new copy has been written and re-read, and deletes only with
`apply=True`. Any doubt about a file means it stays: keeping one too many costs disk,
deleting the wrong one can't be undone.
"""

from __future__ import annotations

import calendar
import errno
import fcntl
import gzip
import logging
import re
import shutil
import sqlite3
import stat
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("repowatch.backup")

# Strict on purpose: rotation only ever sees files it generated itself. Manual copies,
# partial files and renamed backups don't match, so they can never be deleted.
BACKUP_NAME = re.compile(r"^repowatch-(\d{8}T\d{6}Z)\.db\.gz$")
STAMP = "%Y%m%dT%H%M%SZ"
LOCK_NAME = ".rotation.lock"


@dataclass(frozen=True)
class Tiers:
    """How many restore points to keep in each age band."""

    daily: int = 7
    weekly: int = 4
    monthly: int = 6


@dataclass(frozen=True)
class Backup:
    path: Path
    taken_at: datetime
    size: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class RotationResult:
    kept: list[tuple[Backup, str]] = field(default_factory=list)
    removed: list[Backup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def space(self) -> int:
        return sum(b.size for b in self.removed)


class RotationBusy(RuntimeError):
    """Another rotation is already running on this directory."""


def backup_name(now: datetime | None = None) -> str:
    return f"repowatch-{(now or datetime.now(timezone.utc)).strftime(STAMP)}.db.gz"


def copy_database(source: Path, destination: Path) -> None:
    """Write a consistent, verified, compressed copy of `source` to `destination`.

    Work files live in a temporary directory next to the destination, so the final rename
    is atomic and an interrupted run can't leave a truncated copy that looks good.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        raw = Path(tmp) / "backup.db"
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(raw)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        _verify(raw)

        compressed = Path(tmp) / "backup.db.gz"
        with open(raw, "rb") as reader, gzip.open(compressed, "wb") as writer:
            shutil.copyfileobj(reader, writer)
        compressed.replace(destination)


def _verify(path: Path) -> None:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"copy failed integrity check: {result}")
        # An intact but empty copy would pass the check above.
        repos = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        if repos == 0:
            raise RuntimeError("copy has no repos: empty or wrong database?")
    finally:
        conn.close()
    log.info("copy verified: intact, %d repos", repos)


def _parse_name(name: str) -> datetime | None:
    match = BACKUP_NAME.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), STAMP).replace(tzinfo=timezone.utc)
    except ValueError:  # right shape, impossible date
        return None


def inventory(directory: Path) -> list[Backup]:
    """Recognised backups, newest first. Whatever isn't listed here is never deleted."""
    if not directory.is_dir():
        return []
    backups = []
    for entry in directory.iterdir():
        taken_at = _parse_name(entry.name)
        if taken_at is None:
            continue
        info = entry.lstat()
        if stat.S_ISREG(info.st_mode):  # lstat: a symlink is not a backup
            backups.append(Backup(entry, taken_at, info.st_size))
    return sorted(backups, key=lambda b: b.taken_at, reverse=True)


def _months_before(when: datetime, months: int) -> datetime:
    year, month = when.year, when.month - months
    while month <= 0:
        month += 12
        year -= 1
    return when.replace(year=year, month=month, day=min(when.day, calendar.monthrange(year, month)[1]))


def plan_rotation(backups: list[Backup], now: datetime, tiers: Tiers) -> tuple[dict[Path, str], list[Backup]]:
    """Decide what survives, without touching anything: returns (kept path -> reason, to drop).

    Every backup of the last `daily` days; then the newest one of each of the `weekly` ISO
    weeks before that; then the newest one of each of the `monthly` months before that.
    Age comes from the file name, not mtime: copying the directory without `-p` resets
    mtimes, and a rotation trusting them would delete everything or nothing.
    """
    if not backups:
        return {}, []
    ordered = sorted(backups, key=lambda b: b.taken_at, reverse=True)
    # Never end up with no backup at all, whatever the tiers say.
    keep = {ordered[0].path: "newest, never deleted"}

    daily_limit = now - timedelta(days=max(tiers.daily, 0))
    weekly_limit = daily_limit - timedelta(weeks=max(tiers.weekly, 0))
    monthly_limit = _months_before(weekly_limit, max(tiers.monthly, 0))

    weeks: set[tuple[int, int]] = set()
    months: set[tuple[int, int]] = set()
    for backup in ordered:
        taken = backup.taken_at
        if taken > daily_limit:
            keep.setdefault(backup.path, f"daily (last {tiers.daily} days)")
        elif taken > weekly_limit:
            week = tuple(taken.isocalendar()[:2])
            if week not in weeks and len(weeks) < tiers.weekly:
                weeks.add(week)
                keep.setdefault(backup.path, f"weekly ({week[0]}-W{week[1]:02d})")
        elif taken > monthly_limit:
            month = (taken.year, taken.month)
            if month not in months and len(months) < tiers.monthly:
                months.add(month)
                keep.setdefault(backup.path, f"monthly ({month[0]}-{month[1]:02d})")
    return keep, [b for b in ordered if b.path not in keep]


def _is_valid_gzip(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as reader:
            while reader.read(1024 * 1024):
                pass
    except (OSError, EOFError, zlib.error):
        return False
    return True


def _take_lock(directory: Path):
    """Exclusive non-blocking lock: a timer and a manual run must not rotate together."""
    handle = open(directory / LOCK_NAME, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise RotationBusy(f"rotation already running on {directory}") from exc
        raise
    return handle


def rotate(
    directory: Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
    tiers: Tiers | None = None,
) -> RotationResult:
    """Apply the retention policy. Without `apply=True` it only reports what it would do."""
    result = RotationResult(applied=apply)
    if not directory.is_dir():
        return result

    lock = _take_lock(directory) if apply else None
    try:
        backups = inventory(directory)
        keep, drop = plan_rotation(backups, now or datetime.now(timezone.utc), tiers or Tiers())
        result.kept = [(b, keep[b.path]) for b in backups if b.path in keep]
        for backup in drop:
            if not _is_valid_gzip(backup.path):
                result.errors.append(f"{backup.name}: kept, not a valid gzip file")
                log.warning("rotation: keeping %s, not a valid gzip file", backup.name)
                continue
            if apply:
                try:
                    backup.path.unlink()
                except OSError as exc:
                    result.errors.append(f"{backup.name}: could not delete, {exc}")
                    log.error("rotation: could not delete %s: %s", backup.name, exc)
                    continue
            result.removed.append(backup)
    finally:
        if lock:
            lock.close()  # releases the flock

    log.info(
        "rotation (%s): %d kept, %d %s (%s), %d errors",
        "applied" if apply else "dry run",
        len(result.kept),
        len(result.removed),
        "removed" if apply else "to remove",
        format_size(result.space),
        len(result.errors),
    )
    return result


def run(
    source: Path,
    directory: Path,
    *,
    apply: bool = False,
    now: datetime | None = None,
    tiers: Tiers | None = None,
) -> tuple[Path, RotationResult]:
    """Make a copy, verify it, and only then rotate the old ones."""
    destination = directory / backup_name(now)
    copy_database(source, destination)
    log.info("copy saved: %s (%s)", destination.name, format_size(destination.stat().st_size))
    return destination, rotate(directory, apply=apply, now=now, tiers=tiers)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
