"""Daily collector: discovers repos, stores snapshots and generates notable events.

Idempotent: re-running on the same day overwrites today's snapshots and never duplicates
events. Each section of each repo is isolated, so one failure does not stop the run.
"""

from __future__ import annotations

import logging
import sqlite3
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from .config import Config
from .db import add_event
from .github import STAR_ACCEPT, GitHub, GitHubError

log = logging.getLogger(__name__)

TRAFFIC_WINDOW_DAYS = 14


class Collector:
    def __init__(
        self,
        cfg: Config,
        conn: sqlite3.Connection,
        gh: GitHub,
        now: datetime | None = None,
        http: httpx.Client | None = None,
        stars_gh: GitHub | None = None,
    ):
        self.cfg = cfg
        self.conn = conn
        self.gh = gh
        # Since July 2026 listing stargazers is limited to repo admins/collaborators (a token
        # with write access), so an optional separate token can be used for that endpoint only.
        self.stars_gh = stars_gh or gh
        self.stargazers_denied = False
        self.now = now or datetime.now(timezone.utc)
        self.today = self.now.date().isoformat()
        self.now_iso = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.owner = cfg.owner
        self.http = http
        self.errors: list[str] = []
        # Repos seen for the first time in this run: their existing history is imported
        # as a baseline, without generating "new star", "new download"... events.
        self.quiet: set[str] = set()

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        """Collect everything. Returns the number of failed sections."""
        run_id = self.conn.execute(
            "INSERT INTO collector_runs (started_at) VALUES (?)", (self.now_iso,)
        ).lastrowid
        self.conn.commit()

        repos = self.discover()
        for repo in repos:
            self.collect_repo(repo)

        finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE collector_runs SET finished_at=?, repos=?, errors=?, error_log=? WHERE id=?",
            (finished, len(repos), len(self.errors), "\n".join(self.errors) or None, run_id),
        )
        self.conn.commit()
        log.info(
            "done: %d repos, %d errors, %d GitHub requests", len(repos), len(self.errors), self.gh.requests
        )
        return len(self.errors)

    def collect_repo(self, name: str) -> None:
        log.info("collecting %s", name)
        steps: list[tuple[str, Callable[[str], None]]] = [
            ("snapshot", self.collect_snapshot),
            ("stargazers", self.collect_stargazers),
            ("forks", self.collect_forks),
            ("releases", self.collect_releases),
            ("traffic", self.collect_traffic),
            ("popular", self.collect_popular),
            ("activity", self.collect_activity),
            ("ci", self.collect_ci),
        ]
        if name in self.cfg.pypi_packages:
            steps.append(("pypi", self.collect_pypi))
        for section, fn in steps:
            self._section(name, section, fn)

    def _section(self, repo: str, section: str, fn: Callable[[str], None]) -> None:
        try:
            fn(repo)
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001 - one failing section must not stop the run
            self.conn.rollback()
            hint = ""
            if isinstance(exc, GitHubError) and exc.status == 403:
                if section in ("traffic", "popular"):
                    hint = " (the token needs 'Administration: read' on this repo)"
            msg = f"{repo}/{section}: {exc}{hint}"
            log.error(msg)
            log.debug(traceback.format_exc())
            self.errors.append(msg)

    def emit(
        self, repo: str, key: str, type_: str, detail: str, url: str | None = None, when: str | None = None
    ) -> None:
        if repo in self.quiet:
            return
        add_event(self.conn, key, repo, when or self.now_iso, type_, detail, url)

    # ------------------------------------------------------------- discover

    def discover(self) -> list[str]:
        db_was_empty = self.conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
        known = {r[0] for r in self.conn.execute("SELECT name FROM repos")}
        selected: list[str] = []
        for r in self.gh.paginate(f"/users/{self.owner}/repos", {"type": "owner", "sort": "full_name"}):
            if r.get("private"):
                continue
            if r["fork"] and not self.cfg.include_forks:
                continue
            if r["archived"] and not self.cfg.include_archived:
                continue
            if r["name"] in self.cfg.exclude_repos:
                continue
            name = r["name"]
            self.conn.execute(
                """
                INSERT INTO repos (name, description, html_url, language, created_at, pushed_at,
                                   fork, archived, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description, html_url=excluded.html_url,
                    language=excluded.language, pushed_at=excluded.pushed_at,
                    fork=excluded.fork, archived=excluded.archived, last_seen=excluded.last_seen
                """,
                (
                    name, r.get("description"), r.get("html_url"), r.get("language"),
                    r.get("created_at"), r.get("pushed_at"), int(r["fork"]), int(r["archived"]),
                    self.now_iso, self.now_iso,
                ),
            )
            if name not in known:
                self.quiet.add(name)
                if not db_was_empty:
                    add_event(
                        self.conn, f"repo:{name}", name, self.now_iso, "new_repo",
                        f"New public repository detected: {name}", r.get("html_url"),
                    )
            selected.append(name)
        self.conn.commit()
        log.info("discovered %d repos (%d new)", len(selected), len(self.quiet))
        return selected

    # ----------------------------------------------------------- sections

    def _repo_path(self, repo: str) -> str:
        return f"/repos/{self.owner}/{repo}"

    def _is_owner(self, login: str | None) -> bool:
        return bool(login) and login.lower() == self.owner.lower()

    def collect_snapshot(self, repo: str) -> None:
        r = self.gh.get(self._repo_path(repo))
        stars, forks = r["stargazers_count"], r["forks_count"]
        # `watchers_count` is an alias of stars; real watchers are `subscribers_count`.
        watchers, issues = r.get("subscribers_count", 0), r["open_issues_count"]
        prev = self.conn.execute(
            "SELECT watchers FROM repo_snapshots WHERE repo=? AND date<? ORDER BY date DESC LIMIT 1",
            (repo, self.today),
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO repo_snapshots (repo, date, stars, forks, watchers, open_issues)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, date) DO UPDATE SET stars=excluded.stars, forks=excluded.forks,
                watchers=excluded.watchers, open_issues=excluded.open_issues
            """,
            (repo, self.today, stars, forks, watchers, issues),
        )
        if prev and watchers > prev["watchers"]:
            delta = watchers - prev["watchers"]
            self.emit(
                repo, f"watch:{repo}:{self.today}", "watcher",
                f"+{delta} watcher{'s' if delta > 1 else ''} on {repo}", f"{r['html_url']}/watchers",
            )

    def collect_stargazers(self, repo: str) -> None:
        known = {
            row[0]: row[1]
            for row in self.conn.execute("SELECT user, starred_at FROM stargazers WHERE repo=?", (repo,))
        }
        current: set[str] = set()
        try:
            stargazers = list(
                self.stars_gh.paginate(f"{self._repo_path(repo)}/stargazers", accept=STAR_ACCEPT)
            )
        except GitHubError as exc:
            if exc.status not in (401, 403, 404):
                raise
            if not self.stargazers_denied:
                log.warning(
                    "cannot list stargazers (%s): falling back to star counts without names. "
                    "GitHub restricts that list to tokens with write access, see GITHUB_STARS_TOKEN.",
                    exc,
                )
                self.stargazers_denied = True
            self._star_count_event(repo)
            return
        for s in stargazers:
            login = s["user"]["login"]
            current.add(login)
            if login in known:
                continue
            self.conn.execute(
                "INSERT INTO stargazers (repo, user, starred_at) VALUES (?, ?, ?)",
                (repo, login, s.get("starred_at")),
            )
            if not self._is_owner(login):
                self.emit(
                    repo, f"star:{repo}:{login}", "star", f"⭐ {login} starred {repo}",
                    s["user"].get("html_url"), s.get("starred_at"),
                )
        gone = set(known) - current
        self.conn.executemany(
            "DELETE FROM stargazers WHERE repo=? AND user=?", [(repo, u) for u in gone]
        )

    def _star_count_event(self, repo: str) -> None:
        """New stars from the daily counter, when the stargazers list is not accessible."""
        rows = self.conn.execute(
            "SELECT date, stars FROM repo_snapshots WHERE repo=? AND date<=? ORDER BY date DESC LIMIT 2",
            (repo, self.today),
        ).fetchall()
        if len(rows) < 2 or rows[0]["date"] != self.today:
            return
        delta = rows[0]["stars"] - rows[1]["stars"]
        if delta > 0:
            self.emit(
                repo, f"stars:{repo}:{self.today}", "star",
                f"⭐ +{delta} star{'s' if delta > 1 else ''} on {repo}",
                f"https://github.com/{self.owner}/{repo}/stargazers",
            )

    def collect_forks(self, repo: str) -> None:
        known = {r[0] for r in self.conn.execute("SELECT full_name FROM forks WHERE repo=?", (repo,))}
        for f in self.gh.paginate(f"{self._repo_path(repo)}/forks", {"sort": "newest"}):
            if f["full_name"] in known:
                continue
            login = f["owner"]["login"]
            self.conn.execute(
                "INSERT INTO forks (repo, full_name, owner, created_at) VALUES (?, ?, ?, ?)",
                (repo, f["full_name"], login, f.get("created_at")),
            )
            if not self._is_owner(login):
                self.emit(
                    repo, f"fork:{f['full_name']}", "fork", f"🍴 {login} forked {repo}",
                    f.get("html_url"), f.get("created_at"),
                )

    def collect_releases(self, repo: str) -> None:
        for rel in self.gh.paginate(f"{self._repo_path(repo)}/releases"):
            if rel.get("draft"):
                continue
            tag = rel["tag_name"]
            self.conn.execute(
                """
                INSERT INTO releases (repo, tag, name, published_at, prerelease, html_url)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, tag) DO UPDATE SET name=excluded.name,
                    published_at=excluded.published_at, prerelease=excluded.prerelease,
                    html_url=excluded.html_url
                """,
                (repo, tag, rel.get("name"), rel.get("published_at"), int(rel.get("prerelease", False)),
                 rel.get("html_url")),
            )
            for asset in rel.get("assets", []):
                name, count = asset["name"], asset["download_count"]
                prev = self.conn.execute(
                    """
                    SELECT download_count FROM release_asset_snapshots
                    WHERE repo=? AND tag=? AND asset=? AND date<? ORDER BY date DESC LIMIT 1
                    """,
                    (repo, tag, name, self.today),
                ).fetchone()
                self.conn.execute(
                    """
                    INSERT INTO release_asset_snapshots (repo, date, tag, asset, download_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(repo, date, tag, asset) DO UPDATE SET download_count=excluded.download_count
                    """,
                    (repo, self.today, tag, name, count),
                )
                delta = count - (prev["download_count"] if prev else 0)
                if delta > 0:
                    self.emit(
                        repo, f"dl:{repo}:{tag}:{name}:{self.today}", "download",
                        f"⬇️ +{delta} {name} {tag}", rel.get("html_url"),
                    )

    def collect_traffic(self, repo: str) -> None:
        base = f"{self._repo_path(repo)}/traffic"
        views = self.gh.get(f"{base}/views", {"per": "day"})
        clones = self.gh.get(f"{base}/clones", {"per": "day"})
        for v in views.get("views", []):
            self.conn.execute(
                """
                INSERT INTO traffic_daily (repo, date, views, views_uniques) VALUES (?, ?, ?, ?)
                ON CONFLICT(repo, date) DO UPDATE SET views=excluded.views, views_uniques=excluded.views_uniques
                """,
                (repo, v["timestamp"][:10], v["count"], v["uniques"]),
            )
        for c in clones.get("clones", []):
            self.conn.execute(
                """
                INSERT INTO traffic_daily (repo, date, clones, clones_uniques) VALUES (?, ?, ?, ?)
                ON CONFLICT(repo, date) DO UPDATE SET clones=excluded.clones, clones_uniques=excluded.clones_uniques
                """,
                (repo, c["timestamp"][:10], c["count"], c["uniques"]),
            )
        self._detect_spikes(repo, [v["timestamp"][:10] for v in views.get("views", [])])

    def _detect_spikes(self, repo: str, days: list[str]) -> None:
        first_seen = self.conn.execute("SELECT first_seen FROM repos WHERE name=?", (repo,)).fetchone()
        # Only days observed while tracked: older days have no reliable baseline.
        tracked_since = first_seen[0][:10] if first_seen else self.today
        for day in days:
            if day < tracked_since:
                continue
            d = date.fromisoformat(day)
            start = (d - timedelta(days=TRAFFIC_WINDOW_DAYS)).isoformat()
            uniques = self.conn.execute(
                "SELECT views_uniques FROM traffic_daily WHERE repo=? AND date=?", (repo, day)
            ).fetchone()[0]
            total_before = self.conn.execute(
                "SELECT COALESCE(SUM(views_uniques), 0) FROM traffic_daily WHERE repo=? AND date>=? AND date<?",
                (repo, start, day),
            ).fetchone()[0]
            mean = total_before / TRAFFIC_WINDOW_DAYS
            if uniques >= self.cfg.spike_min_uniques and uniques > self.cfg.spike_factor * mean:
                self.emit(
                    repo, f"spike:{repo}:{day}", "spike",
                    f"📈 {uniques} unique visitors on {repo} on {day} (14-day avg {mean:.1f})",
                    f"https://github.com/{self.owner}/{repo}/graphs/traffic",
                )

    def collect_popular(self, repo: str) -> None:
        base = f"{self._repo_path(repo)}/traffic/popular"
        referrers = self.gh.get(f"{base}/referrers")
        paths = self.gh.get(f"{base}/paths")
        seen_before = {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT referrer FROM referrers_snapshots WHERE repo=? AND date<?", (repo, self.today)
            )
        }
        self.conn.execute("DELETE FROM referrers_snapshots WHERE repo=? AND date=?", (repo, self.today))
        self.conn.execute("DELETE FROM paths_snapshots WHERE repo=? AND date=?", (repo, self.today))
        for r in referrers:
            self.conn.execute(
                "INSERT INTO referrers_snapshots (repo, date, referrer, count, uniques) VALUES (?, ?, ?, ?, ?)",
                (repo, self.today, r["referrer"], r["count"], r["uniques"]),
            )
            if r["referrer"] not in seen_before:
                self.emit(
                    repo, f"ref:{repo}:{r['referrer']}", "referrer",
                    f"🔗 First visitors to {repo} from {r['referrer']} ({r['uniques']} unique)",
                )
        for p in paths:
            self.conn.execute(
                "INSERT INTO paths_snapshots (repo, date, path, title, count, uniques) VALUES (?, ?, ?, ?, ?, ?)",
                (repo, self.today, p["path"], p.get("title"), p["count"], p["uniques"]),
            )

    def _since(self, repo: str, kind_clause: str) -> str | None:
        row = self.conn.execute(
            f"SELECT MAX(created_at) FROM activity WHERE repo=? AND {kind_clause}", (repo,)
        ).fetchone()
        if not row or not row[0]:
            return None
        since = datetime.fromisoformat(row[0].replace("Z", "+00:00")) - timedelta(days=1)
        return since.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _store_activity(self, repo: str, kind: str, item: dict, number: int | None, title: str | None) -> None:
        user = item.get("user") or {}
        login = user.get("login")
        is_owner = self._is_owner(login)
        is_bot = user.get("type") == "Bot" or (login or "").endswith("[bot]")
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO activity
                (repo, kind, gh_id, number, user, is_owner, is_bot, title, url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (repo, kind, item["id"], number, login, int(is_owner), int(is_bot), title,
             item.get("html_url"), item.get("created_at")),
        )
        if cur.rowcount and not is_owner and not is_bot:
            label = {"issue": "💬 New issue", "pr": "🔀 New pull request", "comment": "💬 New comment"}[kind]
            where = f"#{number}" if number else ""
            detail = f"{label} on {repo}{where} by {login}"
            if title and kind != "comment":
                detail += f": {title}"
            self.emit(repo, f"{kind}:{item['id']}", kind, detail, item.get("html_url"), item.get("created_at"))

    def collect_activity(self, repo: str) -> None:
        params: dict = {"state": "all", "sort": "created", "direction": "asc"}
        since = self._since(repo, "kind IN ('issue', 'pr')")
        if since:
            params["since"] = since
        for it in self.gh.paginate(f"{self._repo_path(repo)}/issues", params):
            kind = "pr" if "pull_request" in it else "issue"
            self._store_activity(repo, kind, it, it.get("number"), it.get("title"))

        params = {"sort": "created", "direction": "asc"}
        since = self._since(repo, "kind = 'comment'")
        if since:
            params["since"] = since
        for c in self.gh.paginate(f"{self._repo_path(repo)}/issues/comments", params):
            issue_url = c.get("issue_url") or ""
            number = int(issue_url.rsplit("/", 1)[-1]) if issue_url.rsplit("/", 1)[-1].isdigit() else None
            self._store_activity(repo, "comment", c, number, None)

    def collect_ci(self, repo: str) -> None:
        start = (self.now.date() - timedelta(days=TRAFFIC_WINDOW_DAYS)).isoformat()
        known = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT run_id, jobs FROM ci_runs WHERE repo=? AND date>=?", (repo, start)
            )
        }
        runs = self.gh.paginate(
            f"{self._repo_path(repo)}/actions/runs", {"created": f">={start}"}, item_key="workflow_runs"
        )
        for run in runs:
            run_id = run["id"]
            if run_id in known and known[run_id] is not None:
                continue
            jobs = checkout_jobs = None
            if self.cfg.ci_jobs and run.get("status") == "completed":
                job_list = list(
                    self.gh.paginate(
                        f"{self._repo_path(repo)}/actions/runs/{run_id}/jobs", item_key="jobs"
                    )
                )
                jobs = len(job_list)
                checkout_jobs = sum(
                    1
                    for j in job_list
                    if any("checkout" in (s.get("name") or "").lower() for s in j.get("steps") or [])
                )
            self.conn.execute(
                """
                INSERT INTO ci_runs (repo, run_id, date, event, status, jobs, checkout_jobs)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, jobs=excluded.jobs,
                    checkout_jobs=excluded.checkout_jobs
                """,
                (repo, run_id, run["created_at"][:10], run.get("event"), run.get("status"), jobs, checkout_jobs),
            )

    def collect_pypi(self, repo: str) -> None:
        package = self.cfg.pypi_packages[repo]
        client = self.http or httpx.Client(timeout=30, headers={"User-Agent": "repowatch"})
        try:
            resp = client.get(
                f"https://pypistats.org/api/packages/{package.lower()}/overall", params={"mirrors": "false"}
            )
            resp.raise_for_status()
            for row in resp.json().get("data", []):
                if row.get("category") != "without_mirrors":
                    continue
                self.conn.execute(
                    """
                    INSERT INTO pypi_daily (repo, package, date, downloads) VALUES (?, ?, ?, ?)
                    ON CONFLICT(package, date) DO UPDATE SET downloads=excluded.downloads, repo=excluded.repo
                    """,
                    (repo, package, row["date"], row["downloads"]),
                )
        finally:
            if self.http is None:
                client.close()
