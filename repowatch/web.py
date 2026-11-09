"""Read-only dashboard API (plus marking events as seen) and the static single-page UI."""

from __future__ import annotations

import base64
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import Config
from .trends import overview_trends

STATIC_DIR = Path(__file__).parent / "static"
TOP_ASSETS = 8


def create_app(cfg: Config) -> FastAPI:
    cfg.validate_dashboard_auth()
    app = FastAPI(title="RepoWatch", docs_url=None, redoc_url=None, openapi_url=None)

    # Make sure the schema exists even before the first collector run.
    db.connect(cfg.db_path).close()

    @contextmanager
    def conn() -> Iterator[sqlite3.Connection]:
        c = db.connect(cfg.db_path)
        try:
            yield c
        finally:
            c.close()

    if cfg.dashboard_auth == "basic":
        expected = f"{cfg.dashboard_user}:{cfg.dashboard_password}".encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    ok = secrets.compare_digest(base64.b64decode(header[6:], validate=True), expected)
                except ValueError:
                    ok = False
            if not ok:
                return Response(
                    status_code=401, headers={"WWW-Authenticate": 'Basic realm="repowatch"'}
                )
            return await call_next(request)

    @app.get("/api/status")
    def status():
        with conn() as c:
            run = c.execute("SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
            unseen = c.execute("SELECT COUNT(*) FROM events WHERE seen=0").fetchone()[0]
        return {"owner": cfg.owner, "last_run": dict(run) if run else None, "unseen_events": unseen}

    @app.get("/api/events")
    def events(include_seen: bool = False, limit: int = 100):
        where = "" if include_seen else "WHERE seen=0"
        with conn() as c:
            rows = c.execute(
                f"SELECT * FROM events {where} ORDER BY date DESC, id DESC LIMIT ?", (min(limit, 500),)
            ).fetchall()
        return [dict(r) for r in rows]

    class SeenRequest(BaseModel):
        ids: list[int] = []
        all: bool = False

    @app.post("/api/events/seen")
    def mark_seen(body: SeenRequest):
        with conn() as c:
            if body.all:
                c.execute("UPDATE events SET seen=1 WHERE seen=0")
            elif body.ids:
                c.executemany("UPDATE events SET seen=1 WHERE id=?", [(i,) for i in body.ids])
            c.commit()
        return {"ok": True}

    @app.get("/api/summary")
    def summary(days: int = 7):
        cutoff, prev_cutoff = _cutoff(days), _prev_cutoff(days)
        with conn() as c:
            latest_seen = c.execute("SELECT MAX(last_seen) FROM repos").fetchone()[0]
            repos = c.execute(
                "SELECT * FROM repos WHERE last_seen=? ORDER BY name COLLATE NOCASE", (latest_seen,)
            ).fetchall()
            out = []
            for r in repos:
                name = r["name"]
                snap = c.execute(
                    "SELECT * FROM repo_snapshots WHERE repo=? ORDER BY date DESC LIMIT 1", (name,)
                ).fetchone()
                cur = _period(c, name, cutoff)
                release_downloads = _downloads_snapshot(c, name)
                top_ref = c.execute(
                    """
                    SELECT referrer, uniques FROM referrers_snapshots
                    WHERE repo=? AND date=(SELECT MAX(date) FROM referrers_snapshots WHERE repo=?)
                    ORDER BY uniques DESC, count DESC LIMIT 1
                    """,
                    (name, name),
                ).fetchone()
                rel = c.execute(
                    "SELECT tag, published_at, html_url FROM releases WHERE repo=? ORDER BY published_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
                out.append(
                    {
                        "name": name,
                        "description": r["description"],
                        "html_url": r["html_url"],
                        "language": r["language"],
                        "archived": bool(r["archived"]),
                        "pushed_at": r["pushed_at"],
                        "stars": snap["stars"] if snap else None,
                        "forks": snap["forks"] if snap else None,
                        "watchers": snap["watchers"] if snap else None,
                        "open_issues": snap["open_issues"] if snap else None,
                        "downloads": release_downloads["total"],
                        "downloads_as_of": release_downloads["as_of"],
                        **cur,
                        # Same metrics over the window just before this one (None for "all time").
                        "prev": _period(c, name, prev_cutoff, cutoff) if prev_cutoff else None,
                        "top_referrer": dict(top_ref) if top_ref else None,
                        "latest_release": dict(rel) if rel else None,
                    }
                )
        return out

    @app.get("/api/trends")
    def trends(days: int = 14):
        if days < 0 or days > 3660:
            raise HTTPException(422, "days must be between 0 and 3660")
        end = _today()
        start = end - timedelta(days=days) if days else None
        with conn() as c:
            return overview_trends(c, start, end)

    @app.get("/api/repo/{name}")
    def repo_detail(name: str, days: int = 30):
        cutoff = _cutoff(days)
        with conn() as c:
            repo = c.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
            if not repo:
                raise HTTPException(404, "unknown repo")

            traffic = {
                r["date"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM traffic_daily WHERE repo=? AND date>=? ORDER BY date", (name, cutoff)
                )
            }
            ci = {
                r["date"]: dict(r)
                for r in c.execute("SELECT * FROM ci_daily WHERE repo=? AND date>=?", (name, cutoff))
            }
            first = cutoff if days > 0 else min([*traffic, *ci], default=None)
            series = []
            if first:
                d, end = date.fromisoformat(first), _today() - timedelta(days=1)
                while d <= end:
                    k = d.isoformat()
                    t, ci_day = traffic.get(k, {}), ci.get(k, {})
                    series.append(
                        {
                            "date": k,
                            "views": t.get("views"),
                            "views_uniques": t.get("views_uniques"),
                            "clones": t.get("clones"),
                            "clones_uniques": t.get("clones_uniques"),
                            "ci_runs": ci_day.get("runs"),
                            "ci_checkout_jobs": ci_day.get("checkout_jobs"),
                        }
                    )
                    d += timedelta(days=1)

            # Historical totals must come from observed counters; remaining stargazers
            # cannot reconstruct stars that were subsequently removed.
            stars = [
                {"date": r["date"], "stars": r["stars"]}
                for r in c.execute("SELECT date, stars FROM repo_snapshots WHERE repo=? ORDER BY date", (name,))
            ]

            asset_rows = c.execute(
                "SELECT date, tag, asset, download_count FROM release_asset_snapshots WHERE repo=? ORDER BY date",
                (name,),
            ).fetchall()
            latest_counts: dict[str, int] = {}
            for r in asset_rows:
                latest_counts[f"{r['tag']} · {r['asset']}"] = r["download_count"]
            top = sorted(latest_counts, key=latest_counts.get, reverse=True)[:TOP_ASSETS]
            downloads: dict[str, list] = {k: [] for k in top}
            for r in asset_rows:
                key = f"{r['tag']} · {r['asset']}"
                if key in downloads and cutoff <= r["date"] < _today().isoformat():
                    downloads[key].append({"date": r["date"], "count": r["download_count"]})

            releases = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT rel.tag, rel.name, rel.published_at, rel.prerelease, rel.html_url,
                           COALESCE((SELECT SUM(download_count) FROM release_asset_snapshots s
                                     WHERE s.repo=rel.repo AND s.tag=rel.tag
                                       AND s.date=(SELECT MAX(date) FROM release_asset_snapshots
                                                   WHERE repo=rel.repo)), 0) AS downloads
                    FROM releases rel WHERE rel.repo=? ORDER BY rel.published_at DESC
                    """,
                    (name,),
                )
            ]

            ref_date = c.execute(
                "SELECT MAX(date) FROM referrers_snapshots WHERE repo=?", (name,)
            ).fetchone()[0]
            referrers = [
                dict(r)
                for r in c.execute(
                    "SELECT referrer, count, uniques FROM referrers_snapshots WHERE repo=? AND date=? ORDER BY uniques DESC, count DESC",
                    (name, ref_date),
                )
            ]
            referrers_history = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT referrer, MIN(date) first_seen, MAX(date) last_seen, MAX(uniques) max_uniques
                    FROM referrers_snapshots WHERE repo=? GROUP BY referrer ORDER BY first_seen DESC
                    """,
                    (name,),
                )
            ]
            path_date = c.execute("SELECT MAX(date) FROM paths_snapshots WHERE repo=?", (name,)).fetchone()[0]
            paths = [
                dict(r)
                for r in c.execute(
                    "SELECT path, title, count, uniques FROM paths_snapshots WHERE repo=? AND date=? ORDER BY count DESC",
                    (name, path_date),
                )
            ]
            activity = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT kind, number, user, title, url, created_at FROM activity
                    WHERE repo=? AND is_owner=0 AND is_bot=0 ORDER BY created_at DESC LIMIT 50
                    """,
                    (name,),
                )
            ]
            snapshots = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM repo_snapshots WHERE repo=? AND date>=? ORDER BY date", (name, cutoff)
                )
            ]
            pypi = [
                dict(r)
                for r in c.execute(
                    "SELECT date, downloads FROM pypi_daily WHERE repo=? AND date>=? AND date<? ORDER BY date", (name, cutoff, _today().isoformat())
                )
            ]

            prev_cutoff = _prev_cutoff(days)
            period = _period(c, name, cutoff)
            prev = _period(c, name, prev_cutoff, cutoff) if prev_cutoff else None
            release_downloads = _downloads_snapshot(c, name)

        return {
            "repo": dict(repo),
            "period": period,
            "prev": prev,
            "traffic": series,
            "stars": stars,
            "snapshots": snapshots,
            "downloads": downloads,
            "release_downloads": release_downloads,
            "releases": releases,
            "referrers": referrers,
            "referrers_date": ref_date,
            "referrers_history": referrers_history,
            "paths": paths,
            "paths_date": path_date,
            "activity": activity,
            "pypi": pypi,
        }

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _cutoff(days: int) -> str:
    if days <= 0:
        return "0000-00-00"
    return (_today() - timedelta(days=days)).isoformat()


def _prev_cutoff(days: int) -> str | None:
    if days <= 0:
        return None
    return (_today() - timedelta(days=2 * days)).isoformat()


def _period(c: sqlite3.Connection, repo: str, start: str, end: str | None = None) -> dict:
    """Activity in [start, end); end=None excludes the current partial UTC day.

    Daily traffic and PyPI comparisons require every date in the past window.
    Coverage counts stored dates; it does not certify collector section success.
    """
    past = end is not None
    hi = end or _today().isoformat()

    expected_days = (date.fromisoformat(hi) - date.fromisoformat(start)).days if start != "0000-00-00" else None
    coverage = {}
    for source, table in (("traffic", "traffic_daily"), ("pypi", "pypi_daily"), ("ci", "ci_daily")):
        observed = c.execute(
            f"SELECT COUNT(DISTINCT date) FROM {table} WHERE repo=? AND date>=? AND date<?",
            (repo, start, hi),
        ).fetchone()[0]
        coverage[source] = {"observed_days": observed, "expected_days": expected_days,
                            "complete": expected_days is not None and observed == expected_days}

    # Partial current totals remain useful, but incomplete windows must not be compared.
    traffic = dict.fromkeys(("views", "views_uniques", "clones", "clones_uniques"))
    if coverage["traffic"]["observed_days"] and (not past or coverage["traffic"]["complete"]):
        traffic = dict(c.execute(
            """SELECT SUM(views) views, SUM(views_uniques) views_uniques,
                      SUM(clones) clones, SUM(clones_uniques) clones_uniques
               FROM traffic_daily WHERE repo=? AND date>=? AND date<?""",
            (repo, start, hi),
        ).fetchone())
    traffic["ci_runs"] = c.execute(
        "SELECT SUM(runs) FROM ci_daily WHERE repo=? AND date>=? AND date<?", (repo, start, hi)
    ).fetchone()[0]
    pypi = None
    if coverage["pypi"]["observed_days"] and (not past or coverage["pypi"]["complete"]):
        pypi = c.execute(
            "SELECT SUM(downloads) FROM pypi_daily WHERE repo=? AND date>=? AND date<?", (repo, start, hi)
        ).fetchone()[0]

    external = c.execute(
        """
        SELECT COUNT(*) FROM activity
        WHERE repo=? AND is_owner=0 AND is_bot=0 AND created_at>=? AND created_at<?
        """,
        (repo, start, hi),
    ).fetchone()[0]

    return {
        **traffic,
        "window": {"start": start, "end_exclusive": hi, "timezone": "UTC"},
        "coverage": coverage,
        "stars_delta": _stars_delta(c, repo, start, end),
        "downloads_delta": _downloads_delta(c, repo, start, end),
        "pypi_downloads": pypi,
        "external_activity": external,
    }


def _stars_delta(c: sqlite3.Connection, repo: str, start: str, end: str | None = None) -> int | None:
    return _counter_delta(c, "SELECT date, stars v FROM repo_snapshots WHERE repo=?", repo, start, end)


def _downloads_delta(c: sqlite3.Connection, repo: str, start: str, end: str | None = None) -> int | None:
    return _counter_delta(
        c, "SELECT date, SUM(download_count) v FROM release_asset_snapshots WHERE repo=? GROUP BY date",
        repo, start, end,
    )


def _counter_delta(c: sqlite3.Connection, sql: str, repo: str, start: str, end: str | None) -> int | None:
    """Change between the last observations before each exclusive window boundary."""
    rows = c.execute(f"SELECT date, v FROM ({sql}) ORDER BY date", (repo,)).fetchall()
    if not rows:
        return None
    at = lambda d: next((r["v"] for r in reversed(rows) if r["date"] < d), None)  # noqa: E731
    base = at(start)
    if base is None:
        if end is not None:
            return None
        eligible = [r for r in rows if start <= r["date"] < _today().isoformat()]
        if not eligible:
            return None
        base = eligible[0]["v"]  # change observed since tracking began
    top = at(end or _today().isoformat())
    if top is None:
        return None
    return top - base


def _downloads_snapshot(c: sqlite3.Connection, repo: str) -> dict:
    """Latest cumulative release total, including today's observation when available."""
    return dict(c.execute(
        """
        SELECT SUM(download_count) total, MAX(date) as_of FROM release_asset_snapshots
        WHERE repo=? AND date=(SELECT MAX(date) FROM release_asset_snapshots WHERE repo=?)
        """,
        (repo, repo),
    ).fetchone())
