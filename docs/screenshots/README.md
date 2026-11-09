# Documentation screenshots

These are captures of the real dashboard with **synthetic data**. Project names, counts,
referring sites and event text are fictional examples, not the maintainer's analytics.
The dataset includes a launch-like spike, a quiet archived repository, short history and
a missing traffic day. Dates are relative to the UTC date of capture.

The fixture never reads `.env` or the production database and refuses to overwrite an
existing database. The preview binds only to loopback and is stopped after capture.
It is a documentation tool, not a hosted public demo.

To regenerate with Python development dependencies and Node.js installed:

```bash
.venv/bin/pip install -e '.[dev]'
npm install --prefix /tmp/repowatch-doc-tools playwright
/tmp/repowatch-doc-tools/node_modules/.bin/playwright install chromium
PLAYWRIGHT_MODULE=/tmp/repowatch-doc-tools/node_modules/playwright node scripts/capture-screenshots.cjs
```

Optional environment variables select an existing Playwright module, Python interpreter
(`REPOWATCH_SCREENSHOT_PYTHON`) or Chromium binary (`REPOWATCH_CHROMIUM_PATH`).
No GitHub token is needed. Chart.js is loaded from the same CDN as the application.
