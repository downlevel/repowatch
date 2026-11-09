"""Daily observations for the overview; missing observations stay missing."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

METRICS = ("visitors", "stars", "releases", "pypi")


def overview_trends(c: sqlite3.Connection, start: date | None, end: date) -> dict:
    """Return [start, end), considering the same current repositories as the summary."""
    names = [r[0] for r in c.execute(
        "SELECT name FROM repos WHERE last_seen=(SELECT MAX(last_seen) FROM repos) ORDER BY name"
    )]
    if start is None:
        first = c.execute("""
            SELECT MIN(date) FROM (
                SELECT repo, date FROM traffic_daily UNION ALL
                SELECT repo, date FROM repo_snapshots UNION ALL
                SELECT repo, date FROM release_asset_snapshots UNION ALL
                SELECT repo, date FROM pypi_daily
            ) WHERE repo IN (SELECT name FROM repos WHERE last_seen=(SELECT MAX(last_seen) FROM repos))
        """).fetchone()[0]
        start = date.fromisoformat(first) if first else end
    dates = [(start + timedelta(days=i)).isoformat() for i in range(max(0, (end - start).days))]
    repositories = {name: _repo_trends(c, name, dates, start, end) for name in names}
    totals, coverage = [], []
    for i in range(len(dates)):
        values, counts = {}, {}
        for key in METRICS:
            observed = [points[i][key] for points in repositories.values() if points[i][key] is not None]
            values[key] = sum(observed) if observed else None
            counts[key] = len(observed)
        totals.append(values)
        coverage.append(counts)

    # Each repository contributes only its latest 14-day snapshot, never a sum of overlapping windows.
    referrers = [dict(r) for r in c.execute("""
        SELECT s.referrer, SUM(s.count) views, MIN(s.date) oldest_snapshot, MAX(s.date) newest_snapshot
        FROM referrers_snapshots s
        JOIN repos r ON r.name=s.repo
        WHERE r.last_seen=(SELECT MAX(last_seen) FROM repos)
          AND s.date=(SELECT MAX(date) FROM referrers_snapshots WHERE repo=s.repo)
        GROUP BY s.referrer ORDER BY views DESC, s.referrer LIMIT 8
    """)]
    return {"dates": dates, "repositories": repositories, "totals": totals, "coverage": coverage,
            "repository_count": len(names), "referrers": referrers,
            "window": {"start": start.isoformat(), "end_exclusive": end.isoformat(), "timezone": "UTC"}}


def _repo_trends(c: sqlite3.Connection, name: str, dates: list[str], start: date, end: date) -> list[dict]:
    lo, hi = (start - timedelta(days=1)).isoformat(), end.isoformat()
    traffic = {r["date"]: r["views_uniques"] for r in c.execute(
        "SELECT date, views_uniques FROM traffic_daily WHERE repo=? AND date>=? AND date<?", (name, lo, hi)
    )}
    stars = {r["date"]: r["stars"] for r in c.execute(
        "SELECT date, stars FROM repo_snapshots WHERE repo=? AND date>=? AND date<?", (name, lo, hi)
    )}
    assets = {}
    for r in c.execute(
        "SELECT date, tag, asset, download_count FROM release_asset_snapshots WHERE repo=? AND date>=? AND date<?",
        (name, lo, hi),
    ):
        assets.setdefault(r["date"], {})[(r["tag"], r["asset"])] = r["download_count"]
    # All packages observed for the repository must be present before summing a daily value.
    packages = {r[0] for r in c.execute("SELECT DISTINCT package FROM pypi_daily WHERE repo=?", (name,))}
    pypi = {r["date"]: r["downloads"] for r in c.execute(
        """SELECT date, SUM(downloads) downloads FROM pypi_daily WHERE repo=? AND date>=? AND date<?
           GROUP BY date HAVING COUNT(DISTINCT package)=?""", (name, lo, hi, len(packages))
    )}
    out = []
    for day in dates:
        previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        star_change = stars[day] - stars[previous] if day in stars and previous in stars else None
        release_change = None
        if day in assets and previous in assets:
            before, after = assets[previous], assets[day]
            # Deletions and reset counters cannot be interpreted as download flows.
            if before.keys() <= after.keys() and all(after[k] >= v for k, v in before.items()):
                release_change = sum(v - before.get(k, 0) for k, v in after.items())
        out.append({"visitors": traffic.get(day), "stars": star_change,
                    "releases": release_change, "pypi": pypi.get(day)})
    return out
