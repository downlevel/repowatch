from pathlib import Path

from repowatch import cli, scheduler
from repowatch.config import Config


def test_collection_interval_reads_loaded_env_and_cli_override(monkeypatch):
    def load_config(_env_file):
        monkeypatch.setenv("COLLECT_INTERVAL_SECONDS", "900")
        return Config(token="fake", owner="alice", db_path=Path(":memory:"))

    monkeypatch.setattr(cli.Config, "from_env", load_config)
    intervals = []
    monkeypatch.setattr(scheduler, "run", lambda collect, interval: intervals.append(interval) or 0)
    assert cli.main(["collect-loop"]) == 0
    assert cli.main(["collect-loop", "--interval-seconds", "120"]) == 0
    assert intervals == [900, 120]


def test_invalid_env_interval_does_not_start_scheduler(monkeypatch):
    monkeypatch.setattr(cli.Config, "from_env", lambda _: Config(token="fake", owner="alice", db_path=Path(":memory:")))
    monkeypatch.setenv("COLLECT_INTERVAL_SECONDS", "not-an-integer")
    assert cli.main(["collect-loop"]) == 1
