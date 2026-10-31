"""SQLite storage. All dates are UTC, stored as ISO strings (YYYY-MM-DD or full timestamps)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    name         TEXT PRIMARY KEY,
    description  TEXT,
    html_url     TEXT,
    language     TEXT,
    created_at   TEXT,
    pushed_at    TEXT,
    fork         INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

-- Daily snapshot of cumulative counters (last run of the day wins).
CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo         TEXT NOT NULL,
    date         TEXT NOT NULL,
    stars        INTEGER NOT NULL,
    forks        INTEGER NOT NULL,
    watchers     INTEGER NOT NULL,
    open_issues  INTEGER NOT NULL,
    PRIMARY KEY (repo, date)
);

-- GitHub returns the last 14 days on every call: upserted per (repo, date).
CREATE TABLE IF NOT EXISTS traffic_daily (
    repo            TEXT NOT NULL,
    date            TEXT NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0,
    views_uniques   INTEGER NOT NULL DEFAULT 0,
    clones          INTEGER NOT NULL DEFAULT 0,
    clones_uniques  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, date)
);

-- Top 10 referrers / paths, aggregated by GitHub over 14 days, snapshotted daily.
CREATE TABLE IF NOT EXISTS referrers_snapshots (
    repo      TEXT NOT NULL,
    date      TEXT NOT NULL,
    referrer  TEXT NOT NULL,
    count     INTEGER NOT NULL,
    uniques   INTEGER NOT NULL,
    PRIMARY KEY (repo, date, referrer)
);

CREATE TABLE IF NOT EXISTS paths_snapshots (
    repo     TEXT NOT NULL,
    date     TEXT NOT NULL,
    path     TEXT NOT NULL,
    title    TEXT,
    count    INTEGER NOT NULL,
    uniques  INTEGER NOT NULL,
    PRIMARY KEY (repo, date, path)
);

CREATE TABLE IF NOT EXISTS releases (
    repo          TEXT NOT NULL,
    tag           TEXT NOT NULL,
    name          TEXT,
    published_at  TEXT,
    prerelease    INTEGER NOT NULL DEFAULT 0,
    html_url      TEXT,
    PRIMARY KEY (repo, tag)
);

CREATE TABLE IF NOT EXISTS release_asset_snapshots (
    repo            TEXT NOT NULL,
    date            TEXT NOT NULL,
    tag             TEXT NOT NULL,
    asset           TEXT NOT NULL,
    download_count  INTEGER NOT NULL,
    PRIMARY KEY (repo, date, tag, asset)
);

CREATE TABLE IF NOT EXISTS stargazers (
    repo        TEXT NOT NULL,
    user        TEXT NOT NULL,
    starred_at  TEXT,
    PRIMARY KEY (repo, user)
);

CREATE TABLE IF NOT EXISTS forks (
    repo        TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  TEXT,
    PRIMARY KEY (repo, full_name)
);

-- Issues, pull requests and comments (issue + PR conversation comments).
CREATE TABLE IF NOT EXISTS activity (
    repo        TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- issue | pr | comment
    gh_id       INTEGER NOT NULL,
    number      INTEGER,
    user        TEXT,
    is_owner    INTEGER NOT NULL DEFAULT 0,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    title       TEXT,
    url         TEXT,
    created_at  TEXT,
    PRIMARY KEY (kind, gh_id)
);

-- One row per GitHub Actions run; ci_daily aggregates it.
CREATE TABLE IF NOT EXISTS ci_runs (
    repo           TEXT NOT NULL,
    run_id         INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,
    event          TEXT,
    status         TEXT,
    jobs           INTEGER,
    checkout_jobs  INTEGER
);

CREATE VIEW IF NOT EXISTS ci_daily AS
    SELECT repo, date,
           COUNT(*) AS runs,
           COALESCE(SUM(jobs), 0) AS jobs,
           COALESCE(SUM(checkout_jobs), 0) AS checkout_jobs
    FROM ci_runs GROUP BY repo, date;

CREATE TABLE IF NOT EXISTS pypi_daily (
    repo       TEXT NOT NULL,
    package    TEXT NOT NULL,
    date       TEXT NOT NULL,
    downloads  INTEGER NOT NULL,
    PRIMARY KEY (package, date)
);

-- Notable events. `key` makes generation idempotent across repeated runs.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    repo        TEXT,
    date        TEXT NOT NULL,
    type        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    url         TEXT,
    seen        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_seen ON events (seen, date);

-- Sections (per repo) collected successfully at least once. Until then a section imports
-- existing history silently, so a first run that failed or crashed doesn't flood the feed later.
CREATE TABLE IF NOT EXISTS baselines (
    repo     TEXT NOT NULL,
    section  TEXT NOT NULL,
    done_at  TEXT NOT NULL,
    PRIMARY KEY (repo, section)
);

CREATE TABLE IF NOT EXISTS collector_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    repos        INTEGER,
    errors       INTEGER,
    error_log    TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def add_event(
    conn: sqlite3.Connection,
    key: str,
    repo: str | None,
    date: str,
    type_: str,
    detail: str,
    url: str | None = None,
) -> None:
    """Insert an event; if the key exists with a different detail, update it and mark it unseen."""
    conn.execute(
        """
        INSERT INTO events (key, repo, date, type, detail, url) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            seen   = CASE WHEN events.detail != excluded.detail THEN 0 ELSE events.seen END,
            date   = CASE WHEN events.detail != excluded.detail THEN excluded.date ELSE events.date END,
            detail = excluded.detail,
            url    = COALESCE(excluded.url, events.url)
        """,
        (key, repo, date, type_, detail, url),
    )
