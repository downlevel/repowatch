import base64
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from repowatch import db
from repowatch.collector import Collector
from repowatch.config import Config
from tests.test_collector import OWNER, FakeGitHub, routes


def make_client(tmp_path: Path, **cfg_kw) -> TestClient:
    from repowatch.web import create_app

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
    assert summary[0]["views_uniques"] == 14
    detail = client.get("/api/repo/proj?days=30").json()
    assert detail["traffic"][-1]["ci_checkout_jobs"] == 1
    assert detail["stars"] == [{"date": "2026-09-01", "stars": 1}]
    assert client.get("/api/repo/nope").status_code == 404
    assert client.get("/").status_code == 200


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

    # Window [today-14, today-7): the fixture's days 13..8 back, one unique each.
    assert client.get("/api/summary?days=7").json()[0]["prev"]["views_uniques"] == 6
    prev = client.get("/api/summary?days=14").json()[0]["prev"]
    assert prev["views_uniques"] == 14  # backfilled days 28..15 back
    assert prev["downloads_delta"] == 130  # counter 15 days back (150) minus 28 days back (20)
    detail = client.get("/api/repo/proj?days=14").json()
    assert detail["prev"]["views_uniques"] == 14
    assert detail["period"]["views_uniques"] == 14


def test_basic_auth(tmp_path):
    client = make_client(tmp_path, dashboard_user="u", dashboard_password="p")
    assert client.get("/api/status").status_code == 401
    token = base64.b64encode(b"u:p").decode()
    assert client.get("/api/status", headers={"Authorization": f"Basic {token}"}).status_code == 200
