"""Read-only dashboard API (plus marking events as seen) and the static single-page UI."""

from __future__ import annotations

import base64
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .config import Config

STATIC_DIR = Path(__file__).parent / "static"
TOP_ASSETS = 8


def create_app(cfg: Config) -> FastAPI:
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

    if cfg.dashboard_user and cfg.dashboard_password:
        expected = f"{cfg.dashboard_user}:{cfg.dashboard_password}".encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    ok = secrets.compare_digest(base64.b64decode(header[6:]), expected)
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
        cutoff = _cutoff(days)
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
                stars_new = _stars_delta(c, name, cutoff, snap)
                dl_total, dl_delta = _downloads(c, name, cutoff)
                traffic = c.execute(
                    """
                    SELECT COALESCE(SUM(views),0) v, COALESCE(SUM(views_uniques),0) vu,
                           COALESCE(SUM(clones),0) cl, COALESCE(SUM(clones_uniques),0) clu
                    FROM traffic_daily WHERE repo=? AND date>=?
                    """,
                    (name, cutoff),
                ).fetchone()
                ci = c.execute(
                    "SELECT COALESCE(SUM(runs),0) FROM ci_daily WHERE repo=? AND date>=?", (name, cutoff)
                ).fetchone()[0]
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
                external = c.execute(
                    """
                    SELECT COUNT(*) FROM activity
                    WHERE repo=? AND is_owner=0 AND is_bot=0 AND created_at>=?
                    """,
                    (name, cutoff),
                ).fetchone()[0]
                pypi = c.execute(
                    "SELECT SUM(downloads) FROM pypi_daily WHERE repo=? AND date>=?", (name, cutoff)
                ).fetchone()[0]
                out.append(
                    {
                        "name": name,
                        "description": r["description"],
                        "html_url": r["html_url"],
                        "language": r["language"],
                        "archived": bool(r["archived"]),
                        "pushed_at": r["pushed_at"],
                        "stars": snap["stars"] if snap else None,
                        "stars_delta": stars_new,
                        "forks": snap["forks"] if snap else None,
                        "watchers": snap["watchers"] if snap else None,
                        "open_issues": snap["open_issues"] if snap else None,
                        "downloads": dl_total,
                        "downloads_delta": dl_delta,
                        "views": traffic["v"],
                        "views_uniques": traffic["vu"],
                        "clones": traffic["cl"],
                        "clones_uniques": traffic["clu"],
                        "ci_runs": ci,
                        "top_referrer": dict(top_ref) if top_ref else None,
                        "latest_release": dict(rel) if rel else None,
                        "external_activity": external,
                        "pypi_downloads": pypi,
                    }
                )
        return out

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
            first = min([*traffic, *ci], default=None)
            series = []
            if first:
                d, end = date.fromisoformat(first), date.today()
                while d <= end:
                    k = d.isoformat()
                    t, ci_day = traffic.get(k, {}), ci.get(k, {})
                    series.append(
                        {
                            "date": k,
                            "views": t.get("views", 0),
                            "views_uniques": t.get("views_uniques", 0),
                            "clones": t.get("clones", 0),
                            "clones_uniques": t.get("clones_uniques", 0),
                            "ci_runs": ci_day.get("runs", 0),
                            "ci_checkout_jobs": ci_day.get("checkout_jobs", 0),
                        }
                    )
                    d += timedelta(days=1)

            stars, total = [], 0
            for r in c.execute(
                "SELECT substr(starred_at,1,10) d, COUNT(*) n FROM stargazers WHERE repo=? GROUP BY d ORDER BY d",
                (name,),
            ):
                total += r["n"]
                stars.append({"date": r["d"], "stars": total})
            if not stars:  # stargazers list not accessible: use the daily counter
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
                if key in downloads and r["date"] >= cutoff:
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
                    "SELECT date, downloads FROM pypi_daily WHERE repo=? AND date>=? ORDER BY date", (name, cutoff)
                )
            ]

        return {
            "repo": dict(repo),
            "traffic": series,
            "stars": stars,
            "snapshots": snapshots,
            "downloads": downloads,
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


def _cutoff(days: int) -> str:
    if days <= 0:
        return "0000-00-00"
    return (date.today() - timedelta(days=days)).isoformat()


def _stars_delta(c: sqlite3.Connection, repo: str, cutoff: str, snap: sqlite3.Row | None) -> int:
    if c.execute("SELECT 1 FROM stargazers WHERE repo=? LIMIT 1", (repo,)).fetchone():
        return c.execute(
            "SELECT COUNT(*) FROM stargazers WHERE repo=? AND starred_at>=?", (repo, cutoff)
        ).fetchone()[0]
    if not snap:
        return 0
    base = c.execute(
        "SELECT stars FROM repo_snapshots WHERE repo=? AND date<=? ORDER BY date DESC LIMIT 1", (repo, cutoff)
    ).fetchone() or c.execute(
        "SELECT stars FROM repo_snapshots WHERE repo=? ORDER BY date LIMIT 1", (repo,)
    ).fetchone()
    return max(0, snap["stars"] - base["stars"])


def _downloads(c: sqlite3.Connection, repo: str, cutoff: str) -> tuple[int | None, int | None]:
    latest = c.execute(
        "SELECT MAX(date) FROM release_asset_snapshots WHERE repo=?", (repo,)
    ).fetchone()[0]
    if not latest:
        return None, None
    total = c.execute(
        "SELECT SUM(download_count) FROM release_asset_snapshots WHERE repo=? AND date=?", (repo, latest)
    ).fetchone()[0]
    # Baseline: last snapshot on/before the cutoff, or the first one if tracking started later.
    base_date = c.execute(
        "SELECT MAX(date) FROM release_asset_snapshots WHERE repo=? AND date<=?", (repo, cutoff)
    ).fetchone()[0] or c.execute(
        "SELECT MIN(date) FROM release_asset_snapshots WHERE repo=?", (repo,)
    ).fetchone()[0]
    base = c.execute(
        "SELECT SUM(download_count) FROM release_asset_snapshots WHERE repo=? AND date=?", (repo, base_date)
    ).fetchone()[0]
    return total, total - (base or 0)
