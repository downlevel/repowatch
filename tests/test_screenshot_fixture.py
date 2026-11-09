import runpy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from repowatch import db
from repowatch.trends import overview_trends


def test_documentation_fixture_has_no_need_for_real_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    seed = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/screenshot-fixture.py"))["seed_demo"]
    path = tmp_path / "new.db"
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    seed(path, now)
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 4
    assert c.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 12
    trends = overview_trends(c, now.date().replace(day=1), now.date())
    assert trends["repositories"]["repo-lens"][2]["visitors"] is None
    assert trends["repositories"]["tiny-cli"][0]["visitors"] is None
    assert trends["repositories"]["archived-kit"][-1]["visitors"] == 0
    c.close()
    with pytest.raises(FileExistsError):
        seed(path, now)
