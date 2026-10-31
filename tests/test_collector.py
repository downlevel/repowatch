from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repowatch import db
from repowatch.collector import Collector
from repowatch.config import Config
from repowatch.github import GitHubError

OWNER = "alice"
DAY0 = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)


class FakeGitHub:
    """Serves canned responses keyed by path; list values are paginated, others returned by get()."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests = 0

    def _lookup(self, url):
        self.requests += 1
        value = self.routes.get(url)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def get(self, url, params=None, accept=None):
        return self._lookup(url) or {}

    def paginate(self, url, params=None, accept=None, item_key=None):
        data = self._lookup(url) or []
        if item_key:
            data = data.get(item_key, []) if data else []
        yield from data


def user(login, type_="User"):
    return {"login": login, "type": type_, "html_url": f"https://github.com/{login}"}


def traffic(end: datetime, uniques: list[int], key: str):
    days = len(uniques)
    return {
        key: [
            {
                "timestamp": (end - timedelta(days=days - 1 - i)).strftime("%Y-%m-%dT00:00:00Z"),
                "count": u * 2,
                "uniques": u,
            }
            for i, u in enumerate(uniques)
        ]
    }


def routes(now: datetime, *, stars=(), downloads=3, referrers=("google.com",), uniques=None, issues=()):
    base = f"/repos/{OWNER}/proj"
    uniques = uniques or [1] * 14
    return {
        f"/users/{OWNER}/repos": [
            {"name": "proj", "fork": False, "archived": False, "private": False,
             "html_url": "https://github.com/alice/proj", "description": "d", "language": "Python",
             "created_at": "2026-01-01T00:00:00Z", "pushed_at": "2026-09-30T00:00:00Z"},
            {"name": "forked", "fork": True, "archived": False, "private": False},
        ],
        base: {"stargazers_count": len(stars), "forks_count": 0, "subscribers_count": 1,
               "open_issues_count": 0, "html_url": "https://github.com/alice/proj"},
        f"{base}/stargazers": [{"user": user(s), "starred_at": "2026-09-01T00:00:00Z"} for s in stars],
        f"{base}/forks": [],
        f"{base}/releases": [
            {"tag_name": "v1.0.0", "name": "v1", "published_at": "2026-09-01T00:00:00Z",
             "html_url": "https://github.com/alice/proj/releases/v1.0.0",
             "assets": [{"name": "proj.apk", "download_count": downloads}]}
        ],
        f"{base}/traffic/views": traffic(now, uniques, "views"),
        f"{base}/traffic/clones": traffic(now, [5] * 14, "clones"),
        f"{base}/traffic/popular/referrers": [{"referrer": r, "count": 4, "uniques": 2} for r in referrers],
        f"{base}/traffic/popular/paths": [{"path": "/alice/proj", "title": "proj", "count": 4, "uniques": 2}],
        f"{base}/issues": list(issues),
        f"{base}/issues/comments": [],
        f"{base}/actions/runs": {"workflow_runs": [
            {"id": 1, "created_at": now.strftime("%Y-%m-%dT05:00:00Z"), "status": "completed", "event": "push"}
        ]},
        f"{base}/actions/runs/1/jobs": {"jobs": [
            {"steps": [{"name": "Run actions/checkout@v4"}, {"name": "Test"}]},
            {"steps": [{"name": "Lint"}]},
        ]},
    }


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def run(conn, now, **kw):
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    return Collector(cfg, conn, FakeGitHub(routes(now, **kw)), now=now).run()


def events(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def test_first_run_imports_baseline_without_events(conn):
    assert run(conn, DAY0, stars=["bob"]) == 0
    assert events(conn) == []
    assert [r[0] for r in conn.execute("SELECT name FROM repos")] == ["proj"]  # fork excluded
    assert conn.execute("SELECT COUNT(*) FROM traffic_daily").fetchone()[0] == 14
    assert tuple(conn.execute("SELECT runs, jobs, checkout_jobs FROM ci_daily").fetchone()) == (1, 2, 1)


def test_rerun_same_day_is_idempotent(conn):
    run(conn, DAY0)
    run(conn, DAY0)
    assert conn.execute("SELECT COUNT(*) FROM traffic_daily").fetchone()[0] == 14
    assert conn.execute("SELECT COUNT(*) FROM repo_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM referrers_snapshots").fetchone()[0] == 1


def test_next_day_generates_events(conn):
    run(conn, DAY0, stars=["bob"])
    day1 = DAY0 + timedelta(days=1)
    issues = [
        {"id": 10, "number": 1, "title": "bug", "user": user("carol"), "created_at": "2026-10-02T01:00:00Z",
         "html_url": "u1"},
        {"id": 11, "number": 2, "title": "mine", "user": user(OWNER), "created_at": "2026-10-02T01:00:00Z",
         "html_url": "u2"},
        {"id": 12, "number": 3, "title": "deps", "user": user("dependabot[bot]", "Bot"),
         "created_at": "2026-10-02T01:00:00Z", "html_url": "u3", "pull_request": {}},
    ]
    run(conn, day1, stars=["bob", "dave", OWNER], downloads=8,
        referrers=("google.com", "reddit.com"), issues=issues)
    ev = {e["type"]: e["detail"] for e in events(conn)}
    assert set(ev) == {"star", "download", "referrer", "issue"}
    assert "dave" in ev["star"]
    assert ev["download"] == "⬇️ +5 proj.apk v1.0.0"
    assert "reddit.com" in ev["referrer"]
    assert "carol" in ev["issue"]
    # running again the same day must not duplicate anything
    run(conn, day1, stars=["bob", "dave", OWNER], downloads=8,
        referrers=("google.com", "reddit.com"), issues=issues)
    assert len(events(conn)) == 4


def test_spike_detection_only_on_tracked_days(conn):
    run(conn, DAY0, uniques=[50] + [1] * 13)  # old big day before tracking: no spike
    day1 = DAY0 + timedelta(days=1)
    run(conn, day1, uniques=[1] * 13 + [20])
    spikes = [e for e in events(conn) if e["type"] == "spike"]
    assert len(spikes) == 1 and "2026-10-02" in spikes[0]["detail"]


def test_new_repo_event_after_bootstrap(conn):
    run(conn, DAY0)
    day1 = DAY0 + timedelta(days=1)
    r = routes(day1)
    r[f"/users/{OWNER}/repos"].append({"name": "newone", "fork": False, "archived": False, "private": False,
                                       "html_url": "https://github.com/alice/newone"})
    r[f"/repos/{OWNER}/newone"] = {"stargazers_count": 0, "forks_count": 0, "subscribers_count": 0,
                                   "open_issues_count": 0, "html_url": "x"}
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    Collector(cfg, conn, FakeGitHub(r), now=day1).run()
    assert [e["type"] for e in events(conn)] == ["new_repo"]


def test_section_failure_is_isolated(conn):
    r = routes(DAY0)
    r[f"/repos/{OWNER}/proj/traffic/views"] = GitHubError(403, "views", "Resource not accessible")
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    errors = Collector(cfg, conn, FakeGitHub(r), now=DAY0).run()
    assert errors == 1
    assert conn.execute("SELECT COUNT(*) FROM repo_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT errors FROM collector_runs").fetchone()[0] == 1


def test_stargazers_denied_falls_back_to_counts(conn):
    def denied(now, stars):
        r = routes(now, stars=stars)
        r[f"/repos/{OWNER}/proj/stargazers"] = GitHubError(404, "stargazers", "Not Found")
        return r

    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    assert Collector(cfg, conn, FakeGitHub(denied(DAY0, ["bob"])), now=DAY0).run() == 0
    day1 = DAY0 + timedelta(days=1)
    assert Collector(cfg, conn, FakeGitHub(denied(day1, ["bob", "eve", "zoe"])), now=day1).run() == 0
    assert [e["detail"] for e in events(conn)] == ["⭐ +2 stars on proj"]


def test_failed_first_collection_is_still_a_baseline(conn):
    # Day 0: releases and popular fail, so their history isn't imported yet.
    r = routes(DAY0, downloads=16)
    r[f"/repos/{OWNER}/proj/releases"] = GitHubError(500, "releases", "boom")
    r[f"/repos/{OWNER}/proj/traffic/popular/referrers"] = GitHubError(403, "referrers", "nope")
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    assert Collector(cfg, conn, FakeGitHub(r), now=DAY0).run() == 2
    # Day 1: first successful import must not report the whole download count or old referrers.
    day1 = DAY0 + timedelta(days=1)
    assert run(conn, day1, downloads=16) == 0
    assert events(conn) == []
    # Day 2: real changes are reported again.
    run(conn, day1 + timedelta(days=1), downloads=18, referrers=("google.com", "reddit.com"))
    assert sorted(e["type"] for e in events(conn)) == ["download", "referrer"]


def test_crash_after_discovery_is_still_a_baseline(conn):
    # A run that stops right after discovery leaves known repos but no collected sections.
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    Collector(cfg, conn, FakeGitHub(routes(DAY0)), now=DAY0).discover()
    run(conn, DAY0, stars=["bob"], downloads=16)
    assert events(conn) == []


def test_existing_database_is_seeded_from_last_clean_run(conn):
    run(conn, DAY0, downloads=3)
    conn.execute("DELETE FROM baselines")  # as before baselines were tracked
    conn.commit()
    run(conn, DAY0 + timedelta(days=1), downloads=5)
    assert [e["detail"] for e in events(conn)] == ["⬇️ +2 proj.apk v1.0.0"]


def test_stargazer_list_after_counts_only_is_a_baseline(conn):
    r = routes(DAY0, stars=["bob", "eve"])
    r[f"/repos/{OWNER}/proj/stargazers"] = GitHubError(403, "stargazers", "Forbidden")
    cfg = Config(token="x", owner=OWNER, db_path=Path(":memory:"))
    Collector(cfg, conn, FakeGitHub(r), now=DAY0).run()
    # A stars token is added later: the first full list must not report every past stargazer.
    run(conn, DAY0 + timedelta(days=1), stars=["bob", "eve"])
    assert events(conn) == []
