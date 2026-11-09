import base64
import pytest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from repowatch import db
from repowatch.collector import Collector
from repowatch.config import Config
from tests.test_collector import OWNER, FakeGitHub, routes


def make_client(tmp_path: Path, **cfg_kw) -> TestClient:
    from repowatch.web import create_app

    cfg_kw.setdefault("dashboard_auth", "external")
    cfg = Config(token="x", owner=OWNER, db_path=tmp_path / "w.db", **cfg_kw)
    conn = db.connect(cfg.db_path)
    now = datetime.now(timezone.utc)
    Collector(cfg, conn, FakeGitHub(routes(now, stars=["bob"])), now=now).run()
    conn.close()
    return TestClient(create_app(cfg))


def test_summary_and_detail(tmp_path):
    client = make_client(tmp_path)
    summary = client.get("/api/summary?days=14").json()
    assert summary[0]["name"] == "proj"
    assert summary[0]["downloads"] == 3
    assert summary[0]["views_uniques"] == 13
    detail = client.get("/api/repo/proj?days=30").json()
    assert detail["traffic"][0]["views"] is None
    assert detail["traffic"][-1]["ci_checkout_jobs"] is None
    assert detail["traffic"][-1]["date"] == (date.today() - timedelta(days=1)).isoformat()
    assert detail["stars"] == [{"date": datetime.now(timezone.utc).date().isoformat(), "stars": 1}]
    assert client.get("/api/repo/nope").status_code == 404
    assert client.get("/").status_code == 200
    trends = client.get("/api/trends?days=7").json()
    assert len(trends["dates"]) == 7
    assert trends["repository_count"] == 1
    assert trends["totals"][-1]["visitors"] == 1
    assert client.get("/api/trends?days=-1").status_code == 422


def test_previous_period(tmp_path):
    client = make_client(tmp_path)
    # The fixture only has the last 14 days: no full previous window to compare with yet.
    prev = client.get("/api/summary?days=7").json()[0]["prev"]
    assert prev["views_uniques"] is None and prev["downloads_delta"] is None
    assert client.get("/api/summary?days=0").json()[0]["prev"] is None

    # Backfill older history: 1 unique visitor/day and a release counter growing 10/day.
    conn = db.connect(tmp_path / "w.db")
    for back in range(15, 30):
        d = (date.today() - timedelta(days=back)).isoformat()
        conn.execute("INSERT INTO traffic_daily (repo, date, views, views_uniques) VALUES ('proj', ?, 2, 1)", (d,))
        conn.execute(
            "INSERT INTO release_asset_snapshots VALUES ('proj', ?, 'v1', 'a.tar.gz', ?)", (d, 300 - back * 10)
        )
    conn.commit()
    conn.close()

    # The missing day 14 back leaves a gap in the previous 7-day window.
    assert client.get("/api/summary?days=7").json()[0]["prev"]["views_uniques"] is None
    prev = client.get("/api/summary?days=14").json()[0]["prev"]
    assert prev["views_uniques"] == 14  # backfilled days 28..15 back
    assert prev["downloads_delta"] == 140  # last counter before end (150) minus last before start (10)
    detail = client.get("/api/repo/proj?days=14").json()
    assert detail["prev"]["views_uniques"] == 14
    assert detail["period"]["views_uniques"] == 13
    assert detail["period"]["coverage"]["traffic"] == {"observed_days": 13, "expected_days": 14, "complete": False}


def test_basic_auth(tmp_path):
    client = make_client(tmp_path, dashboard_auth="basic", dashboard_user="u", dashboard_password="p")
    assert client.get("/api/status").status_code == 401
    token = base64.b64encode(b"u:p").decode()
    assert client.get("/api/status", headers={"Authorization": f"Basic {token}"}).status_code == 200
    for path in ("/", "/app.js", "/api/trends", "/api/repo/proj"):
        assert client.get(path).status_code == 401
    assert client.post("/api/events/seen", json={"all": True}).status_code == 401
    for header in ("Basic !!!", "Basic /w==", "Bearer abc"):
        assert client.get("/api/status", headers={"Authorization": header}).status_code == 401


@pytest.mark.parametrize("options", [
    {}, {"dashboard_user": "u"}, {"dashboard_password": "p"},
    {"dashboard_auth": "none"},
    {"dashboard_user": "u:p", "dashboard_password": "p"},
])
def test_dashboard_fails_closed(tmp_path, options):
    from repowatch.web import create_app

    with pytest.raises(ValueError):
        create_app(Config(token="x", owner=OWNER, db_path=tmp_path / "absent.db", **options))
    assert not (tmp_path / "absent.db").exists()


def test_external_auth_is_explicit(tmp_path):
    client = make_client(tmp_path, dashboard_auth="external")
    assert client.get("/api/status").status_code == 200


def test_complete_utc_windows_and_missing_days(tmp_path, monkeypatch):
    import repowatch.web as web

    monkeypatch.setattr(web, "_today", lambda: date(2026, 11, 8))
    assert web._cutoff(7) == "2026-11-01"
    assert web._prev_cutoff(7) == "2026-10-25"
    c = db.connect(tmp_path / "coverage.db")
    for day in range(25, 32):
        c.execute("INSERT INTO traffic_daily (repo, date, views_uniques) VALUES ('proj', ?, 0)", (f"2026-10-{day}",))
    # An observed zero counts as coverage; an absent row does not.
    complete = web._period(c, "proj", "2026-10-25", "2026-11-01")
    assert complete["views_uniques"] == 0
    assert complete["coverage"]["traffic"]["complete"] is True
    c.execute("DELETE FROM traffic_daily WHERE date='2026-10-28'")
    incomplete = web._period(c, "proj", "2026-10-25", "2026-11-01")
    assert incomplete["views_uniques"] is None
    assert incomplete["coverage"]["traffic"]["observed_days"] == 6
    c.execute("INSERT INTO traffic_daily (repo, date, views_uniques) VALUES ('proj', '2026-11-08', 999)")
    assert web._period(c, "proj", "2026-11-01")["views_uniques"] is None
    c.close()


def test_star_counter_loss_and_exclusive_boundary(tmp_path, monkeypatch):
    import repowatch.web as web

    monkeypatch.setattr(web, "_today", lambda: date(2026, 11, 8))
    c = db.connect(tmp_path / "stars.db")
    for day, stars in (("2026-10-31", 10), ("2026-11-07", 8), ("2026-11-08", 100)):
        c.execute("INSERT INTO repo_snapshots (repo, date, stars, forks, watchers, open_issues) VALUES ('proj', ?, ?, 0, 0, 0)", (day, stars))
    assert web._stars_delta(c, "proj", "2026-11-01") == -2
    assert web._stars_delta(c, "proj", "2026-10-25", "2026-11-01") is None
    c.close()


def test_latest_release_total_is_distinct_from_period_increase(tmp_path, monkeypatch):
    import repowatch.web as web

    monkeypatch.setattr(web, "_today", lambda: date(2026, 11, 8))
    client = make_client(tmp_path)
    c = db.connect(tmp_path / "w.db")
    c.execute("DELETE FROM release_asset_snapshots WHERE repo='proj'")
    for day, count in (("2026-10-30", 6), ("2026-11-07", 501), ("2026-11-08", 672)):
        c.execute("INSERT INTO release_asset_snapshots VALUES ('proj', ?, 'v1.0.0', 'app.apk', ?)", (day, count))
        c.execute("INSERT INTO release_asset_snapshots VALUES ('proj', ?, 'v1.0.0', 'legacy.apk', 5)", (day,))
    c.commit()
    c.close()

    for days in (7, 14, 0):
        summary = client.get(f"/api/summary?days={days}").json()[0]
        assert summary["downloads"] == 677
        assert summary["downloads_as_of"] == "2026-11-08"
        assert summary["downloads_delta"] == 495  # 506 through yesterday minus initial 11.
        detail = client.get(f"/api/repo/proj?days={days}").json()
        assert detail["release_downloads"] == {"total": 677, "as_of": "2026-11-08"}
        assert detail["period"]["downloads_delta"] == 495
