# RepoWatch

Daily history of the GitHub metrics that GitHub itself forgets: stars, traffic, clones,
referrers and release downloads for **every public repo of an account**, plus a small
self-hosted dashboard for understanding interest in your projects.

GitHub only shows totals (stars, download counts) or a rolling 14-day window (traffic).
RepoWatch snapshots everything once a day into SQLite, so you get:

- **History beyond 14 days** for views, unique visitors, clones and referrers.
- **Observed counter changes**: net star balance, including losses, and release download trends.
- **Clones next to observed CI activity**: checkout estimates from job step names provide
  context for automated activity; the difference does not measure human clones.
- **A "what's new" feed**: new stars (and who), forks, watchers, issues/PRs/comments from
  people other than you, new downloads, first visitors from a new site, traffic spikes,
  newly published repos.
- **A dashboard you can read at a glance**: totals, selectable daily trends, sparklines,
  referring sites and a searchable, sortable repository table. Works on a phone with
  system, light and dark themes.

## How it works

```
cron (daily) ──▶ repowatch collect ──▶ GitHub REST API
                        │
                        ▼
                 data/repowatch.db (SQLite)
                        │
                        ▼
          repowatch serve (FastAPI + static page with Chart.js)
```

- Repos are discovered on every run (`GET /users/{owner}/repos`): no hardcoded list.
  Forks are excluded by default.
- Traffic arrives as a 14-day series and is upserted per `(repo, date)`: re-runs never
  duplicate and up to 14 missed days are recovered automatically.
- Each section of each repo is collected independently: one failure doesn't stop the run.
- The first successful collection of each section of a repo imports its existing history as
  a baseline, without flooding the feed with "new star" events. This holds per section, so a
  first run that crashed or a section that failed (e.g. a missing token permission) doesn't
  dump the whole history into the feed once it starts working.

## Setup

Use Docker Engine with the Compose plugin, or Python 3.11+ for a manual installation.

### Docker Compose (recommended)

```bash
git clone https://github.com/downlevel/repowatch.git
cd repowatch
cp .env.example .env
```

Set `GITHUB_OWNER`, `GITHUB_TOKEN`, `DASHBOARD_USER` and `DASHBOARD_PASSWORD` in `.env`, then:

```bash
docker compose up -d --build
```

Open **http://127.0.0.1:8765** and sign in with the dashboard credentials. Collection starts
automatically and repeats every six hours; history persists in a named Docker volume.
No TeleLogin account or external login service is required.

See [self-hosting instructions](docs/self-hosting.md) for authentication modes, HTTPS,
backups, upgrades and troubleshooting.

### Manual installation

```bash
git clone https://github.com/downlevel/repowatch.git
cd repowatch
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # then edit it
```

### GitHub token

Create a **fine-grained personal access token** (Settings → Developer settings → Fine-grained tokens):

- Resource owner: the account to monitor
- Repository access: *All repositories* (or the ones you want)
- Repository permissions: **Administration: read-only** (needed for traffic) and
  **Metadata: read-only** (default).
- Nothing else.

**Who starred your repos.** Since July 2026 GitHub limits the stargazers list
(`GET /repos/{o}/{r}/stargazers`) to repository admins and collaborators: a fine-grained token
needs **Contents: read and write**, a classic token the `public_repo` scope. That's write access,
which a read-only tool shouldn't need, so by default RepoWatch detects new stars from the daily
counter ("+2 stars on repo"), without names. If you want names and dates and accept the
trade-off, put a separate write-capable token in `GITHUB_STARS_TOKEN`: it is used for that
endpoint only.

Put it in `.env` as `GITHUB_TOKEN`, together with `GITHUB_OWNER`. Traffic endpoints only
work for repos you have admin access to, so you can monitor your own account or an
organisation you administer. See `.env.example` for all options.

### First run

```bash
.venv/bin/repowatch collect     # imports the last 14 days of traffic right away
.venv/bin/repowatch serve       # http://127.0.0.1:8765
```

Exit codes of `collect`: `0` ok, `2` some sections failed (see the log), `1` fatal error.
A run uses roughly 12 requests per repo plus one per new CI run, well within the
5,000 requests/hour limit.

### Daily schedule

```cron
0 6 * * * cd /opt/repowatch && .venv/bin/repowatch collect >> /var/log/repowatch.log 2>&1
```

GitHub traffic days are UTC; the latest day is partial until it ends and is refreshed on the next run.

### Dashboard

Pick a period (7d, 14d, 30d, 90d, All). Period metrics use complete UTC days;
today is excluded because it is partial. Choose System, Light or Dark at the top;
the dashboard remembers the theme and period in your browser.

**Overview**, in reading order:

1. **Four totals across current repositories**: summed daily unique visitors, net star
   balance, downloads (observed release counter changes plus PyPI) and external activity
   (issues, PRs and comments from other people, bots excluded). Comparisons use repositories
   with comparable observations; the caption states when this is a subset.
2. **Overall trend**: switch between visitors, star balance, release downloads and PyPI.
   Daily totals sum available observations; the tooltip reports the contributing repository
   count. Missing days break the line. Daily counter changes require consecutive observations.
   Deleted release assets and reset download counters leave gaps rather than negative downloads.
3. **Referring sites**: bars use each repository's latest 14-day GitHub snapshot. This block
   is independent of the period selector, shows snapshot dates and never sums overlapping snapshots.
4. **Repositories**: search names and descriptions, filter archived repositories, recorded
   inactivity or incomplete traffic, and sort by name or a metric using the column headers.
   The release total shows cumulative GitHub downloads at the latest snapshot (including today
   when available); the period column shows observed increases through yesterday plus PyPI,
   excluding the initial baseline. Sparklines follow the trend metric. Traffic coverage appears under each name. On a phone,
   use the sort selector and scroll the table horizontally to see all metrics.
5. **What's new**: unseen events grouped by repository. Mark them seen individually or
   together; past events stay in History. This feed tracks unseen events, independent of the
   period selector.

**Repo detail**: totals, visits, clones and observed CI checkouts, historical star counters,
asset downloads and PyPI, plus referring sites, popular pages, releases and external activity.
Snapshot and cumulative sections state their own windows rather than implying that every
value is a flow in the selected period.

Missing observations are distinct from zero. Traffic and PyPI comparisons require full daily
coverage in the previous window; current incomplete daily coverage suppresses those comparisons.
Coverage counts stored dates and does not certify that every collector section succeeded.
Release counters and star balances can cover only the observations available since tracking
began. "All" displays the stored history without a previous-period comparison.

The design mockup is in `docs/design/repowatch-redesign.html`.

### Dashboard deployment

`repowatch serve` binds to `127.0.0.1` by default. Choose authentication explicitly:

- `DASHBOARD_AUTH=basic` (default): both `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are
  required. Missing credentials prevent startup; the dashboard and all APIs are protected.
- `DASHBOARD_AUTH=external`: authentication is handled by your existing reverse proxy.
  Protect every path and restrict direct backend access. TeleLogin is optional.

Use HTTPS for remote access. The UI works under a URL prefix; see
`deploy/nginx.conf.example` and the [self-hosting guide](docs/self-hosting.md).

A systemd unit is in `deploy/repowatch-web.service`.

### Backups (optional)

GitHub only serves 14 days of traffic, so the database can't be rebuilt once lost.
`repowatch backup` writes a compressed copy to `data/backups/` using SQLite's online backup
API, re-reads it to verify it, and only then rotates old copies: all from the last 7 days,
then one per week for 4 weeks and one per month for 6 months (`BACKUP_KEEP_*`).

```bash
repowatch backup --apply        # copy and rotate (without --apply rotation is a dry run)
repowatch backup --rotate-only  # show what rotation would remove
repowatch backup --list
gunzip -c data/backups/repowatch-<timestamp>.db.gz > data/repowatch.db   # restore
```

`deploy/repowatch-backup.*` runs this nightly; `deploy/repowatch-backup-r2.*` then uploads the
copies to Cloudflare R2 with `rclone copy --immutable`, which never deletes anything remotely
(set your remote and bucket in the unit, and expire old copies with a bucket lifecycle rule).

### PyPI downloads (optional)

Set `PYPI_PACKAGES=repo:package,...` to also store daily downloads from
[pypistats.org](https://pypistats.org) (without mirrors).

## Data model

| Table | Content |
|---|---|
| `repos` | discovered repos, `first_seen` / `last_seen` |
| `repo_snapshots` | daily stars, forks, watchers, open issues |
| `traffic_daily` | views, unique visitors, clones, unique cloners per day |
| `referrers_snapshots`, `paths_snapshots` | daily snapshot of GitHub's 14-day top 10 |
| `releases`, `release_asset_snapshots` | releases and daily cumulative download count per asset |
| `stargazers`, `forks` | who starred / forked, and when |
| `activity` | issues, PRs and comments, flagged owner / bot |
| `ci_runs` (+ view `ci_daily`) | Actions runs, jobs and jobs using `actions/checkout` |
| `pypi_daily` | PyPI downloads per day |
| `events` | the "what's new" feed |
| `baselines` | sections already collected once per repo (see "How it works") |
| `collector_runs` | run log |

It's plain SQLite: query it directly for anything the dashboard doesn't show.

## Caveats

- "Unique visitors" over a period is the sum of daily uniques (GitHub doesn't expose
  uniques over arbitrary windows), so a person visiting on two days counts twice.
- Referrers and popular pages are GitHub's top 10 only.
- Watchers are reported as a count; GitHub doesn't timestamp subscriptions.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## License

MIT
