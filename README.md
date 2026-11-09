# RepoWatch

Daily history of the GitHub metrics that GitHub itself forgets: stars, traffic, clones,
referrers and release downloads for **every public repo of an account**, plus a small
self-hosted dashboard that tells you whether *real people* are finding and using your projects.

GitHub only shows totals (stars, download counts) or a rolling 14-day window (traffic).
RepoWatch snapshots everything once a day into SQLite, so you get:

- **History beyond 14 days** for views, unique visitors, clones and referrers.
- **Deltas from cumulative counters**: new stars and new release downloads per day/asset.
- **Clones next to CI checkouts**: every GitHub Actions job using `actions/checkout`
  is a clone, so raw clone counts are mostly noise. RepoWatch counts them so you can tell.
- **A "what's new" feed**: new stars (and who), forks, watchers, issues/PRs/comments from
  people other than you, new downloads, first visitors from a new site, traffic spikes,
  newly published repos.
- **A dashboard you can read at a glance**: four totals with the change vs the previous
  period, then where it happened, then the details. Works on a phone, light and dark.

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

Requires Python 3.11+.

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

Pick a period (7d, 14d, 30d, 90d, All) at the top right; every number follows it.

**Overview**, in reading order:

1. **Four totals across all repos**: unique visitors, new stars, downloads (new release-asset
   downloads plus PyPI) and external activity (issues, PRs and comments from people other than
   you, bots excluded). Each shows the change vs the previous period of the same length:
   green for gains, grey with ↓ for drops (a quiet week isn't an alarm).
2. **What's new**: unseen events grouped by repo, one glyph per type (★ star, ⑂ fork,
   ↓ download, ↗ new referrer, ▲ traffic spike, + new repo, ◉ watcher). Issues (!), PRs (⇄)
   and comments (“) are blue: they may need a reply. Mark events as seen one by one or all
   at once; past events stay in History.
3. **Repositories by activity**: three numbers per repo (visitors, new stars, downloads) with
   their change. Repos with nothing in the period are folded into a *Quiet* group.

**Repo detail**: stars, visitors, downloads and clones with their change; one large visits
chart; clones vs CI checkouts, cumulative stars, downloads per asset and PyPI downloads (only
if configured); referring sites, popular pages, every referrer seen, releases and external
activity in tabs (an accordion on a phone).

The change vs the previous period appears once the database covers that whole window: with a
14-day period, data back to 28 days ago. The first run imports 14 days of traffic, so visitor
changes show up after about two weeks of daily runs; stars and downloads start counting from
the first run, so theirs take the full 28 days. Until then the tiles say "no earlier data to
compare". "All" shows totals without comparison.

The design mockup is in `docs/design/repowatch-redesign.html`.

### Dashboard deployment

`repowatch serve` binds to `127.0.0.1` by default. To reach it from outside either:

- set `DASHBOARD_USER` / `DASHBOARD_PASSWORD` (built-in HTTP basic auth), and/or
- put it behind a reverse proxy with authentication (see `deploy/nginx.conf.example`;
  the UI uses relative URLs, so it works under a sub-path).

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
