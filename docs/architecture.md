# Architecture and storage

```text
Docker collector loop (six hours) ──▶ repowatch collect ──▶ GitHub REST API
Manual installation: cron ────────────────┘                    │
                                                              ▼
                                                        SQLite database
                                                              │
                                                              ▼
                                          FastAPI + JavaScript dashboard
```

Repositories are discovered on each run. Each section is collected independently so a
failure does not stop other sections. Traffic is upserted by repository and UTC day. Each
first successful section collection is imported as a baseline without generating a flood
of old events. Failed sections remain unbaselined until a later successful collection.

## SQLite tables

| Table | Content |
| --- | --- |
| `repos` | Discovered public repositories and first/last observation |
| `repo_snapshots` | Daily stars, forks, watchers and open issue counters |
| `traffic_daily` | Daily views, unique visitors, clones and unique cloners |
| `referrers_snapshots`, `paths_snapshots` | Snapshots of GitHub's 14-day top entries |
| `releases`, `release_asset_snapshots` | Releases and cumulative asset download counters |
| `stargazers`, `forks` | Observed people/events when listing is available |
| `activity` | Issues, PRs and comments, with owner/bot flags |
| `ci_runs`, `ci_daily` | Observed Actions runs and checkout estimates |
| `pypi_daily` | Optional daily PyPI downloads |
| `events` | Dismissible dashboard feed |
| `baselines` | Successfully imported sections per repository |
| `collector_runs` | Collection times and errors |

SQLite runs in WAL mode. Use the SQLite backup API for consistent copies; a standalone
copy of the `.db` file can omit committed transactions still present in the WAL.

## Authentication and deployment

`DASHBOARD_AUTH=basic` validates both credentials before opening the database and applies
authentication to the entire application. `external` explicitly delegates protection to
the reverse proxy; identity headers are not interpreted by RepoWatch.

The Docker web container receives the account name and dashboard credentials, while the
collector receives the GitHub token and collection settings. Both share the data volume.
The image build context excludes local credentials, Git history and databases.
