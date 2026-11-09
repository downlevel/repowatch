from __future__ import annotations

import gzip
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repowatch import backup, cli, db

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
TIERS = backup.Tiers(daily=7, weekly=4, monthly=6)


def add_repo(conn, name: str) -> None:
    conn.execute(
        "INSERT INTO repos (name, first_seen, last_seen) VALUES (?, '2026-01-01', '2026-01-01')",
        (name,),
    )
    conn.commit()


@pytest.fixture
def database(tmp_path) -> Path:
    path = tmp_path / "repowatch.db"
    conn = db.connect(path)
    add_repo(conn, "one")
    conn.close()
    return path


def restore(copy: Path, tmp_path: Path) -> sqlite3.Connection:
    target = tmp_path / "restored.db"
    with gzip.open(copy, "rb") as reader:
        target.write_bytes(reader.read())
    return sqlite3.connect(target)


def days_ago(n: int) -> datetime:
    return (NOW - timedelta(days=n)).replace(hour=4, minute=40, second=0, microsecond=0)


def fake_backup(directory: Path, when: datetime, content: bytes = b"fake") -> Path:
    """A real gzip: rotation validates candidates, so random bytes would be kept."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / backup.backup_name(when)
    with gzip.open(path, "wb") as writer:
        writer.write(content)
    return path


def populate(directory: Path, days) -> dict[int, Path]:
    return {n: fake_backup(directory, days_ago(n)) for n in days}


def rotate(directory: Path, **kwargs) -> backup.RotationResult:
    return backup.rotate(directory, now=NOW, tiers=TIERS, **kwargs)


def present(directory: Path) -> list[Path]:
    return [b.path for b in backup.inventory(directory)]


def test_copy_is_a_real_database(database, tmp_path):
    copy, _ = backup.run(database, tmp_path / "backups")
    conn = restore(copy, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_copy_includes_transactions_still_in_the_wal(database, tmp_path):
    live = db.connect(database)
    add_repo(live, "two")  # committed to the -wal, not yet to the .db
    copy, _ = backup.run(database, tmp_path / "backups")
    live.close()
    conn = restore(copy, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2
    conn.close()


def test_missing_database_is_an_error(tmp_path):
    with pytest.raises(sqlite3.Error):
        backup.run(tmp_path / "missing.db", tmp_path / "backups")


def test_copy_without_repos_is_not_kept(tmp_path):
    empty = tmp_path / "empty.db"
    db.connect(empty).close()
    directory = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="no repos"):
        backup.run(empty, directory)
    assert list(directory.iterdir()) == []


def test_failed_copy_does_not_rotate(tmp_path):
    directory = tmp_path / "backups"
    old = fake_backup(directory, days_ago(300))
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(sqlite3.Error):
        backup.run(empty, directory, apply=True)
    assert old.exists()


def test_last_seven_days_all_stay(tmp_path):
    directory = tmp_path / "backups"
    copies = populate(directory, range(0, 10))
    result = rotate(directory, apply=True)
    remaining = set(present(directory))
    assert all(copies[n] in remaining for n in range(0, 7))
    assert result.errors == []


def test_one_backup_per_week_then_per_month(tmp_path):
    directory = tmp_path / "backups"
    populate(directory, range(0, 365))
    rotate(directory, apply=True)
    remaining = backup.inventory(directory)
    assert len(remaining) <= 7 + 4 + 6

    older = [b for b in remaining if b.taken_at <= NOW - timedelta(days=7)]
    weekly = [b for b in older if b.taken_at > NOW - timedelta(days=35)]
    weeks = [b.taken_at.isocalendar()[:2] for b in weekly]
    assert len(weeks) == len(set(weeks)) <= 4

    monthly = [b for b in older if b.taken_at <= NOW - timedelta(days=35)]
    months = [(b.taken_at.year, b.taken_at.month) for b in monthly]
    assert len(months) == len(set(months)) <= 6


def test_newest_is_never_deleted(tmp_path):
    directory = tmp_path / "backups"
    only = fake_backup(directory, days_ago(400))
    result = backup.rotate(directory, apply=True, now=NOW, tiers=backup.Tiers(0, 0, 0))
    assert only.exists() and result.removed == []


def test_dry_run_touches_nothing_and_matches_real_run(tmp_path):
    directory = tmp_path / "backups"
    populate(directory, range(0, 40))
    before = set(present(directory))

    dry = rotate(directory)
    assert set(present(directory)) == before
    assert dry.removed and dry.applied is False and dry.space > 0

    real = rotate(directory, apply=True)
    assert {b.name for b in dry.removed} == {b.name for b in real.removed}
    assert not any(b.path.exists() for b in real.removed)


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        "repowatch-manual.db.gz",
        "repowatch-20260101T000000Z.db.gz.partial",
        "repowatch-20260101T000000Z.db",
        "repowatch-20260231T000000Z.db.gz",  # 31 February
        "otherapp-20260101T000000Z.db.gz",
    ],
)
def test_foreign_names_are_ignored(tmp_path, name):
    directory = tmp_path / "backups"
    populate(directory, range(0, 40))
    foreign = directory / name
    foreign.write_bytes(b"do not delete")
    rotate(directory, apply=True)
    assert foreign.exists()
    assert foreign not in present(directory)


def test_directories_and_symlinks_are_not_backups(tmp_path):
    directory = tmp_path / "backups"
    populate(directory, range(0, 3))
    subdir = directory / backup.backup_name(days_ago(300))
    subdir.mkdir()
    target = tmp_path / "elsewhere.db.gz"
    target.write_bytes(b"precious")
    link = directory / backup.backup_name(days_ago(301))
    link.symlink_to(target)

    rotate(directory, apply=True)

    assert subdir.is_dir() and link.is_symlink() and target.exists()


def test_corrupt_backup_is_kept_and_reported_without_stopping_the_rest(tmp_path):
    directory = tmp_path / "backups"
    populate(directory, range(0, 40))
    broken = directory / backup.backup_name(days_ago(300))
    broken.write_bytes(b"\x1f\x8b\x08\x00 and then garbage")

    result = rotate(directory, apply=True)

    assert broken.exists()
    assert result.removed
    assert any("gzip" in e for e in result.errors)


def test_two_rotations_do_not_overlap(tmp_path):
    directory = tmp_path / "backups"
    populate(directory, range(0, 40))
    held = backup._take_lock(directory)
    with pytest.raises(backup.RotationBusy):
        rotate(directory, apply=True)
    held.close()
    assert rotate(directory, apply=True).removed
    assert (directory / backup.LOCK_NAME) not in present(directory)


def test_missing_directory_is_not_an_error(tmp_path):
    result = rotate(tmp_path / "never-created", apply=True)
    assert result.removed == [] and result.errors == []


def test_age_comes_from_the_name_not_mtime(tmp_path):
    directory = tmp_path / "backups"
    old = fake_backup(directory, days_ago(300))
    fake_backup(directory, days_ago(0))
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))
    result = rotate(directory, apply=True)
    assert [b.name for b in result.removed] == [old.name]


def test_new_copy_rotates_old_ones_only_with_apply(database, tmp_path):
    directory = tmp_path / "backups"
    old = fake_backup(directory, days_ago(300))

    copy, _ = backup.run(database, directory, tiers=TIERS)
    assert copy.exists() and old.exists()

    _, result = backup.run(database, directory, apply=True, tiers=TIERS)
    assert not old.exists() and result.applied is True


def _cli(tmp_path, monkeypatch, database, *args) -> int:
    env = tmp_path / "test.env"
    env.write_text("")
    monkeypatch.setenv("REPOWATCH_DB", str(database))
    monkeypatch.setenv("REPOWATCH_BACKUP_DIR", str(tmp_path / "cli-backups"))
    return cli.main(["--env-file", str(env), "backup", *args])


def test_cli_backup_and_list(database, tmp_path, monkeypatch, capsys):
    assert _cli(tmp_path, monkeypatch, database, "--apply") == 0
    assert len(present(tmp_path / "cli-backups")) == 1
    assert _cli(tmp_path, monkeypatch, database, "--list") == 0
    assert "repowatch-" in capsys.readouterr().out


def test_cli_returns_1_on_failure(tmp_path, monkeypatch):
    assert _cli(tmp_path, monkeypatch, tmp_path / "missing.db") == 1
