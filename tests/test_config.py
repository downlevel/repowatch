from repowatch.config import Config


def test_auth_defaults_to_basic_and_accepts_explicit_external(tmp_path, monkeypatch):
    for name in ("DASHBOARD_AUTH", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env(tmp_path / "absent.env")
    assert cfg.dashboard_auth == "basic"
    monkeypatch.setenv("DASHBOARD_AUTH", "external")
    assert Config.from_env(tmp_path / "absent.env").dashboard_auth == "external"
