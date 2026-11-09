from datetime import date

from repowatch import db
from repowatch.trends import overview_trends


def add_repo(c, name, last_seen="2026-11-08"):
    c.execute("INSERT INTO repos (name, first_seen, last_seen) VALUES (?, '2026-10-01', ?)", (name, last_seen))


def test_daily_gaps_coverage_and_current_repository_scope(tmp_path):
    c = db.connect(tmp_path / "trends.db")
    for name in ("alpha", "beta"):
        add_repo(c, name)
    add_repo(c, "old", "2026-11-01")
    for name, day, visitors in (("alpha", "2026-11-06", 0), ("alpha", "2026-11-07", 5),
                                ("beta", "2026-11-07", 2), ("alpha", "2026-11-08", 999),
                                ("old", "2026-11-07", 999)):
        c.execute("INSERT INTO traffic_daily (repo, date, views_uniques) VALUES (?, ?, ?)", (name, day, visitors))
    result = overview_trends(c, date(2026, 11, 5), date(2026, 11, 8))
    assert result["dates"] == ["2026-11-05", "2026-11-06", "2026-11-07"]
    assert [p["visitors"] for p in result["totals"]] == [None, 0, 7]
    assert [p["visitors"] for p in result["coverage"]] == [0, 1, 2]
    assert result["repository_count"] == 2
    assert "old" not in result["repositories"]
    assert overview_trends(c, None, date(2026, 11, 8))["window"]["start"] == "2026-11-06"
    c.close()


def test_counter_losses_resets_deleted_assets_and_new_assets(tmp_path):
    c = db.connect(tmp_path / "trends.db")
    add_repo(c, "alpha")
    for day, stars in (("2026-11-03", 10), ("2026-11-04", 8), ("2026-11-06", 12), ("2026-11-07", 12)):
        c.execute("INSERT INTO repo_snapshots VALUES ('alpha', ?, ?, 0, 0, 0)", (day, stars))
    for day, assets in (("2026-11-03", {"a": 10}), ("2026-11-04", {"a": 15, "b": 2}),
                        ("2026-11-05", {"b": 4}), ("2026-11-06", {"b": 1}),
                        ("2026-11-07", {"b": 3})):
        for asset, count in assets.items():
            c.execute("INSERT INTO release_asset_snapshots VALUES ('alpha', ?, 'v1', ?, ?)", (day, asset, count))
    points = overview_trends(c, date(2026, 11, 4), date(2026, 11, 8))["repositories"]["alpha"]
    assert [p["stars"] for p in points] == [-2, None, None, 0]
    assert [p["releases"] for p in points] == [7, None, None, 2]
    c.close()


def test_pypi_package_coverage_and_non_overlapping_referrers(tmp_path):
    c = db.connect(tmp_path / "trends.db")
    for name in ("alpha", "beta"):
        add_repo(c, name)
    for package, day, count in (("a", "2026-11-06", 3), ("b", "2026-11-06", 5), ("a", "2026-11-07", 9)):
        c.execute("INSERT INTO pypi_daily VALUES ('alpha', ?, ?, ?)", (package, day, count))
    for repo, day, count in (("alpha", "2026-11-06", 100), ("alpha", "2026-11-07", 7), ("beta", "2026-11-06", 3)):
        c.execute("INSERT INTO referrers_snapshots VALUES (?, ?, 'reddit.com', ?, 1)", (repo, day, count))
    result = overview_trends(c, date(2026, 11, 6), date(2026, 11, 8))
    assert [p["pypi"] for p in result["totals"]] == [8, None]
    assert result["referrers"] == [{"referrer": "reddit.com", "views": 10,
                                    "oldest_snapshot": "2026-11-06", "newest_snapshot": "2026-11-07"}]
    c.close()


def test_empty_history(tmp_path):
    c = db.connect(tmp_path / "trends.db")
    result = overview_trends(c, None, date(2026, 11, 8))
    assert result["dates"] == []
    assert result["totals"] == []
    assert result["repositories"] == {}
    assert result["referrers"] == []
    c.close()
