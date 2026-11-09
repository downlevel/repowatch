import gzip
import os
from pathlib import Path
import subprocess
import sys

import pytest

from repowatch import backup, db


@pytest.mark.parametrize("valid", [True, False])
def test_manual_restore_recipe_verifies_before_replacing_database(tmp_path, valid):
    target = tmp_path / "history.db"
    c = db.connect(target)
    c.execute("INSERT INTO repos (name,first_seen,last_seen) VALUES ('original','2026-11-09','2026-11-09')")
    c.commit()
    c.close()
    source = tmp_path / "copy.db.gz"
    backup.copy_database(target, source)
    c = db.connect(target)
    c.execute("INSERT INTO repos (name,first_seen,last_seen) VALUES ('later','2026-11-09','2026-11-09')")
    c.commit()
    c.close()
    if not valid:
        source.write_bytes(gzip.compress(b"not a SQLite backup"))
    guide = (Path(__file__).resolve().parents[1] / "docs/manual-installation.md").read_text()
    recipe = guide.split(".venv/bin/python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    env = dict(os.environ, REPOWATCH_DB=str(target), BACKUP_PATH=str(source))
    result = subprocess.run([sys.executable, "-c", recipe], cwd=tmp_path, env=env, capture_output=True, text=True)
    c = db.connect(target)
    names = [r[0] for r in c.execute("SELECT name FROM repos ORDER BY name")]
    c.close()
    if valid:
        assert result.returncode == 0, result.stderr
        assert names == ["original"]
    else:
        assert result.returncode != 0
        assert names == ["later", "original"]
