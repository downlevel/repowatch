"""Container-local check of the API, using configured Basic authentication."""

from __future__ import annotations

import httpx

from .config import Config


def main() -> int:
    cfg = Config.from_env()
    auth = (cfg.dashboard_user, cfg.dashboard_password) if cfg.dashboard_auth == "basic" else None
    try:
        cfg.validate_dashboard_auth()
        response = httpx.get("http://127.0.0.1:8765/api/status", auth=auth, timeout=5, trust_env=False)
        response.raise_for_status()
        return 0 if "owner" in response.json() else 1
    except (httpx.HTTPError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
