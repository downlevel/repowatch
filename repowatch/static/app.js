"use strict";

const view = document.getElementById("view");
const charts = [];
const mobile = matchMedia("(max-width: 720px)");
const FEED_LIMIT = 5;
let days = 14;
try { days = Number(localStorage.getItem("repowatch.days") ?? 14); } catch (_) { /* storage blocked */ }
let events = [];
let showAllEvents = false;
let activeTab = "ref";
let renderVersion = 0;
let renderedMobile = mobile.matches;
let resizeTimer;
if (![0, 7, 14, 30, 90].includes(days)) days = 14;

// ------------------------------------------------------------- helpers

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const periodShort = () => (days ? `${days}d` : "all time");
const periodLabel = () => (days ? `last ${days} complete UTC days` : "all time");
const compareLabel = () => (days ? `vs prev ${days}d` : "all time");
const today = () => new Date().toISOString().slice(0, 10);
const cutoff = () => (days ? new Date(Date.now() - days * 86400000).toISOString().slice(0, 10) : "0000-00-00");
const shortDate = (iso) => (iso ? new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "—");

function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 60) return `${Math.round(s / 86400)}d ago`;
  return iso.slice(0, 10);
}

async function api(path, opts) {
  const res = await fetch(`api/${path}`, opts);
  // An auth proxy redirected the call to its login page: reload so the browser follows it.
  if (res.redirected && !new URL(res.url).pathname.includes("/api/")) {
    location.reload();
    return new Promise(() => {});
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

const markSeen = (body) =>
  api("events/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function clearCharts() {
  while (charts.length) charts.pop().destroy();
}

// ------------------------------------------------------------- metrics

// Change vs the previous window, or null when there is nothing comparable.
function change(cur, prev, pct) {
  if (!days || cur === null || cur === undefined || prev === null || prev === undefined) return null;
  const diff = cur - prev;
  if (!diff) return { txt: "no change", cls: "" };
  const arrow = diff > 0 ? "↑" : "↓";
  const size = pct && prev > 0 ? `${Math.round((Math.abs(diff) / prev) * 100)}%` : fmt(Math.abs(diff));
  return { txt: `${arrow} ${size}`, cls: diff > 0 ? "pos" : "" };
}

// New release-asset + PyPI downloads. Null when a source present in `ref` has no data in `p`,
// so a window is never compared with one that lacks part of the count.
function newDownloads(p, ref = p) {
  if (!p) return null;
  let n = null;
  for (const k of ["downloads_delta", "pypi_downloads"]) {
    if (ref[k] === null || ref[k] === undefined) continue;
    if (p[k] === null || p[k] === undefined) return null;
    n = (n ?? 0) + p[k];
  }
  return n;
}

const METRICS = [
  { label: "Daily unique visitors (sum)", short: "Visitors", pct: true, get: (p) => p?.views_uniques,
    hint: "Sum of daily unique visitors: a person visiting on two days counts twice." },
  { label: "Star balance", short: "Star balance", get: (p) => p?.stars_delta },
  { label: "Downloads in period", short: "Downloads", pct: true, get: (p, ref) => newDownloads(p, ref),
    hint: "Observed increase through yesterday (UTC), excluding downloads already present at the first observation. Combines release counters and PyPI. The separate release total uses the latest snapshot, including today when available." },
];
const EXTERNAL_METRIC = { label: "External activity", get: (p) => p?.external_activity,
  hint: "Issues, pull requests and comments from people other than you (bots excluded)." };

function previousMetric(r, m) {
  if (!r.prev) return null;
  if (m === METRICS[0] && (!r.coverage?.traffic?.complete || !r.prev.coverage?.traffic?.complete)) return null;
  if (m === METRICS[2] && r.pypi_downloads != null && (!r.coverage?.pypi?.complete || !r.prev.coverage?.pypi?.complete)) return null;
  return m.get(r.prev, r);
}

// Totals over all repos; the change only uses repos that also have data for the previous window.
function total(repos, m) {
  let value = null, curCmp = 0, prevCmp = 0, comparable = 0;
  for (const r of repos) {
    const v = m.get(r, r);
    if (v === null || v === undefined) continue;
    value = (value ?? 0) + v;
    const p = previousMetric(r, m);
    if (p !== null && p !== undefined) { curCmp += v; prevCmp += p; comparable += 1; }
  }
  return { value, d: comparable ? change(curCmp, prevCmp, m.pct) : null, comparable };
}

const isQuiet = (r) => [r.views_uniques, r.stars_delta, newDownloads(r), r.external_activity].every((v) => v === 0);

// ------------------------------------------------------------- charts

function chart(canvas, type, labels, datasets, { horizontal = false, contributors, repositoryCount } = {}) {
  const grid = css("--border");
  const tick = css("--fg-3");
  const font = { size: 11 };
  const valueAxis = { beginAtZero: true, grid: { color: grid }, border: { display: false }, ticks: { color: tick, font, maxTicksLimit: 4, precision: 0 } };
  const labelAxis = { grid: { display: false }, border: { color: grid }, ticks: { color: horizontal ? css("--fg-2") : tick, font, maxTicksLimit: 7, maxRotation: 0 } };
  if (horizontal) {
    const max = mobile.matches ? 18 : 32;
    labelAxis.ticks.callback = function (v) {
      const s = this.getLabelForValue(v);
      return s.length > max ? `${s.slice(0, max - 1)}…` : s;
    };
  }
  const c = new Chart(canvas, {
    type,
    data: { labels, datasets: datasets.map((d) => ({ borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.25, borderRadius: 2, ...d })) },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: horizontal ? "y" : "x",
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false, callbacks: { afterBody: (items) => contributors && items.length ? `${contributors[items[0].dataIndex]} / ${repositoryCount} repositories observed` : "" } } },
      scales: horizontal ? { x: valueAxis, y: labelAxis } : { x: labelAxis, y: valueAxis },
    },
  });
  charts.push(c);
  return c;
}

function alpha(hex, a) {
  const n = parseInt(hex.replace("#", ""), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// ------------------------------------------------------------- shared pieces

const GLYPHS = { star: "★", fork: "⑂", download: "↓", referrer: "↗", spike: "▲", new_repo: "+", watcher: "◉", issue: "!", pr: "⇄", comment: "“" };
const EXTERNAL = new Set(["issue", "pr", "comment"]);
const glyph = (type) => `<span class="glyph${EXTERNAL.has(type) ? " ext" : ""}" aria-hidden="true">${GLYPHS[type] || "•"}</span>`;

// The collector writes self-contained sentences ("⭐ +2 stars on proj"): drop the emoji (the
// glyph replaces it) and the repo name (shown next to the event).
function eventText(e) {
  let t = String(e.detail || "").replace(/^\p{Extended_Pictographic}️?\s*/u, "");
  if (e.repo) {
    const r = e.repo.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    t = t.replace(new RegExp(` (?:on|to) ${r}(?=#)`), " ")
      .replace(new RegExp(` (?:on|to) ${r}(?=[\\s:]|$)`), "")
      .replace(new RegExp(`(starred|forked) ${r}(?=\\s|$)`), "$1")
      .replace(new RegExp(`: ${r}$`), "");
  }
  return t;
}

function eventRow(e, { showRepo, dismiss }) {
  const text = esc(eventText(e));
  const repo = showRepo && e.repo ? `<a class="repo" href="#/repo/${encodeURIComponent(e.repo)}">${esc(e.repo)}</a>` : "";
  const body = e.url ? `<a class="text" href="${esc(e.url)}" target="_blank" rel="noopener">${text}</a>` : `<span class="text">${text}</span>`;
  return `<div class="event">${glyph(e.type)}
    <div class="body"><div>${repo}${body}</div><span class="meta">${esc(dismiss ? ago(e.date) : shortDate(e.date))}</span></div>
    ${dismiss ? `<button class="seen" data-id="${e.id}" title="Mark as seen" aria-label="Mark as seen">✓</button>` : ""}
  </div>`;
}

const deltaPill = (d) => (d ? `<span class="pill ${d.cls}">${esc(d.txt)}</span>` : "");

// Without a comparable previous window the "vs prev" caption would read as a missing pill.
function kpiTile(label, value, d, caption, hint, extra = "") {
  const cap = !d && days && caption.startsWith("vs prev") ? "no earlier data to compare" : caption;
  return `<div class="card kpi">
    <div class="l">${esc(label)}${hint ? `<details class="metric-help"><summary aria-label="About ${esc(label)}">ⓘ</summary><p>${esc(hint)}</p></details>` : ""}</div>
    <div class="v">${fmt(value)}</div>
    <div class="d">${deltaPill(d)}<span>${esc(cap)}</span></div>
    ${extra}
  </div>`;
}

// ------------------------------------------------------------- overview

const TREND_METRICS = {
  visitors: { label: "Visitors", title: "Daily unique visitors (sum)", color: "--visitors", hint: "Daily uniques summed across repositories. The same person can be counted on multiple days and repositories." },
  stars: { label: "Star balance", title: "Daily star balance", color: "--stars", hint: "Net change between consecutive daily counters, including losses. A missing counter leaves a gap." },
  releases: { label: "Release downloads", title: "Daily release downloads", color: "--downloads", hint: "Changes in asset counters on consecutive days. Deleted assets and reset counters leave gaps. Includes checksums and other release assets." },
  pypi: { label: "PyPI downloads", title: "Daily PyPI downloads", color: "--downloads", hint: "Daily downloads across observed packages. Missing package observations leave a gap." },
};
let overviewMetric = "visitors";
let overviewSort = "visitors";
let overviewDirection = "desc";
let overviewSearch = "";
let overviewFilter = "all";

function sparkline(values, color, label) {
  const known = values.filter((v) => v != null);
  if (!known.length) return '<span class="spark-empty">No daily observations</span>';
  const min = Math.min(0, ...known), max = Math.max(1, ...known);
  const x = (i) => values.length === 1 ? 60 : 3 + i * 114 / (values.length - 1);
  const y = (v) => 31 - (v - min) / (max - min) * 26;
  let path = "", gap = true;
  const dots = [];
  values.forEach((v, i) => {
    if (v == null) { gap = true; return; }
    path += `${gap ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    if ((i === 0 || values[i - 1] == null) && (i === values.length - 1 || values[i + 1] == null)) {
      dots.push(`<circle cx="${x(i)}" cy="${y(v)}" r="2"/>`);
    }
    gap = false;
  });
  return `<svg class="sparkline" viewBox="0 0 120 36" role="img" aria-label="${esc(label)}: ${known.length} observed days out of ${values.length}" style="color:${esc(color)}">
    <path d="M3,${y(0)} L117,${y(0)}" class="spark-baseline"/>
    <path d="${path}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>${dots.join("")}</svg>`;
}

async function renderOverview(version) {
  const [evs, repos, trends] = await Promise.all([
    api("events?limit=100"), api(`summary?days=${days}`), api(`trends?days=${days}`),
  ]);
  if (version !== renderVersion) return;
  clearCharts();
  events = evs;
  const keys = ["visitors", "stars", "releases"];
  const kpis = [...METRICS, EXTERNAL_METRIC].map((m, i) => {
    const t = total(repos, m);
    const key = keys[i];
    const trend = key ? `<div class="kpi-trend">${sparkline(trends.totals.map((p) => p[key]), css(TREND_METRICS[key].color), TREND_METRICS[key].title)}<span>${i === 2 ? "Release downloads only" : "Daily observations"}</span></div>`
      : '<p class="kpi-context">Issues · pull requests · comments</p>';
    const caption = t.d && t.comparable < repos.length ? `${compareLabel()} · ${t.comparable}/${repos.length} repos compared` : compareLabel();
    return kpiTile(m.label, t.value, t.d, caption, m.hint, trend);
  });
  view.innerHTML = `
    <div class="overview-heading"><div><p class="eyebrow">YOUR PROJECTS AT A GLANCE</p><h1>Overview</h1></div>
      <p class="meta">${esc(periodLabel())}<br>UTC · today excluded (partial)</p></div>
    <section class="kpis">${kpis.join("")}</section>
    <div class="insights-grid">
      <section class="card chart-card main" aria-label="Overall trend">
        <div class="chart-top"><span class="card-title">Overall trend</span>
          <label class="select-label">Metric <select id="trend-metric">${Object.entries(TREND_METRICS).map(([key, m]) => `<option value="${key}"${key === overviewMetric ? " selected" : ""}>${esc(m.label)}</option>`).join("")}</select></label></div>
        <p class="note" id="trend-note"></p><div id="overview-chart" class="chart-box"></div>
        <p class="meta" id="trend-coverage"></p>
      </section>
      <section class="card chart-card sources-card">
        <div class="chart-top"><span class="card-title">Where visits come from</span><span class="badge">14-day snapshots</span></div>
        <p class="note">Views from each repository’s latest GitHub snapshot. Independent of the selected period; GitHub returns only top sources.</p>
        ${trends.referrers.length ? `<div class="source-bars">${trends.referrers.map((r) => `<div class="source-row"><div><span>${esc(r.referrer)}</span><b>${fmt(r.views)}</b></div><div class="source-track"><span style="width:${r.views / Math.max(1, trends.referrers[0].views) * 100}%"></span></div></div>`).join("")}</div>
          <p class="meta">Snapshots: ${esc(shortDate(trends.referrers.reduce((a, r) => r.oldest_snapshot < a ? r.oldest_snapshot : a, trends.referrers[0].oldest_snapshot)))} – ${esc(shortDate(trends.referrers.reduce((a, r) => r.newest_snapshot > a ? r.newest_snapshot : a, trends.referrers[0].newest_snapshot)))}</p>`
          : '<div class="empty"><b>No referring sites yet</b><span>Sources appear after GitHub reports traffic.</span></div>'}
      </section>
    </div>
    <section class="card repositories-card" aria-label="Repositories">
      <div class="card-head repo-toolbar"><div><span class="card-title">Repositories</span><span class="meta" id="repo-count"></span></div>
        <div class="repo-controls"><label class="search-label"><span class="sr-only">Search repositories</span><input id="repo-search" type="search" placeholder="Find a repository…" value="${esc(overviewSearch)}"></label>
          <label class="select-label"><span class="sr-only">Filter repositories</span><select id="repo-filter">
            <option value="all">All repositories</option><option value="current">Not archived</option><option value="archived">Archived</option><option value="quiet">No recorded activity</option><option value="missing">Incomplete traffic</option></select></label>
          <label class="select-label mobile-sort">Sort <select id="repo-sort"><option value="visitors">Visitors</option><option value="stars">Star balance</option><option value="release_total">Release total</option><option value="downloads">Downloads in period</option><option value="external">External activity</option><option value="name">Name</option></select></label></div></div>
      <p class="mobile-table-hint">Swipe the table to see all metrics →</p>
      <div class="table-wrap" tabindex="0" role="region" aria-label="Repository metrics, scroll horizontally for more columns"><table class="repo-table"><thead><tr>
        <th data-sort="name"><button data-sort="name">Repository <span></span></button></th><th>Daily trend <span id="row-trend-label"></span></th>
        <th class="r" data-sort="visitors"><button data-sort="visitors">Visitors <span></span></button></th>
        <th class="r" data-sort="stars"><button data-sort="stars">Star balance <span></span></button></th>
        <th class="r" data-sort="release_total"><button data-sort="release_total">Release total <span></span></button></th>
        <th class="r" data-sort="downloads"><button data-sort="downloads">In period <span></span></button></th>
        <th class="r" data-sort="external"><button data-sort="external">External <span></span></button></th>
      </tr></thead><tbody id="repo-rows"></tbody></table></div>
      <p class="table-note">Release total: cumulative GitHub downloads at the latest snapshot, including today when available. In period: observed increase through yesterday plus PyPI, excluding the initial baseline. Visitors are summed daily uniques. Dashes mean no observations; period counts may cover only part of the window.</p>
    </section>
    <section class="card" id="feed"></section>`;
  renderFeed();
  const filter = document.getElementById("repo-filter");
  filter.value = overviewFilter;
  filter.addEventListener("change", () => { overviewFilter = filter.value; renderRepositoryTable(repos, trends); });
  document.getElementById("repo-search").addEventListener("input", (e) => { overviewSearch = e.target.value; renderRepositoryTable(repos, trends); });
  document.getElementById("repo-sort").addEventListener("change", (e) => {
    overviewSort = e.target.value; overviewDirection = overviewSort === "name" ? "asc" : "desc"; renderRepositoryTable(repos, trends);
  });
  document.querySelectorAll("button[data-sort]").forEach((b) => b.addEventListener("click", () => {
    overviewDirection = overviewSort === b.dataset.sort && overviewDirection === "desc" ? "asc" : "desc";
    overviewSort = b.dataset.sort;
    renderRepositoryTable(repos, trends);
  }));
  document.getElementById("trend-metric").addEventListener("change", (e) => {
    overviewMetric = e.target.value; renderOverviewChart(trends); renderRepositoryTable(repos, trends);
  });
  renderOverviewChart(trends);
  renderRepositoryTable(repos, trends);
}

function renderOverviewChart(trends) {
  clearCharts();
  const m = TREND_METRICS[overviewMetric];
  const values = trends.totals.map((p) => p[overviewMetric]);
  const observed = values.filter((v) => v != null).length;
  document.getElementById("trend-note").textContent = m.hint;
  const el = document.getElementById("overview-chart");
  const fullDays = trends.coverage.filter((p) => p[overviewMetric] === trends.repository_count).length;
  document.getElementById("trend-coverage").textContent = `${observed} / ${trends.dates.length} days observed · ${fullDays} days cover all ${trends.repository_count} repositories. Daily totals can include different repositories; missing days are gaps.`;
  if (!observed) {
    el.innerHTML = '<div class="empty"><b>No daily observations for this metric yet</b><span>Counter changes need two consecutive daily snapshots. Try Visitors or another period.</span></div>';
    return;
  }
  el.innerHTML = '<canvas id="c-overview" role="img"></canvas>';
  const canvas = document.getElementById("c-overview");
  canvas.setAttribute("aria-label", `${m.title}. ${observed} observed days out of ${trends.dates.length}.`);
  const color = css(m.color);
  chart(canvas, "line", trends.dates.map((d) => shortDate(d).replace(/,? \d{4}$/, "")), [
    { label: m.title, data: values, borderColor: color, backgroundColor: alpha(color, 0.09), fill: true, pointRadius: 2, spanGaps: false },
  ], { contributors: trends.coverage.map((p) => p[overviewMetric]), repositoryCount: trends.repository_count });
}

function renderRepositoryTable(repos, trends) {
  const metrics = { visitors: METRICS[0], stars: METRICS[1], downloads: METRICS[2], external: EXTERNAL_METRIC };
  const get = (r) => overviewSort === "name" ? r.name : overviewSort === "release_total" ? r.downloads : metrics[overviewSort].get(r, r);
  const search = overviewSearch.trim().toLocaleLowerCase();
  const selected = repos.filter((r) => (!search || `${r.name} ${r.description || ""}`.toLocaleLowerCase().includes(search))
    && (overviewFilter !== "current" || !r.archived) && (overviewFilter !== "archived" || r.archived)
    && (overviewFilter !== "quiet" || isQuiet(r)) && (overviewFilter !== "missing" || !r.coverage?.traffic?.complete));
  selected.sort((a, b) => {
    const av = get(a), bv = get(b);
    if (av == null && bv != null) return 1;
    if (bv == null && av != null) return -1;
    const cmp = overviewSort === "name" ? av.localeCompare(bv) : (av ?? 0) - (bv ?? 0);
    return (overviewDirection === "asc" ? cmp : -cmp) || a.name.localeCompare(b.name);
  });
  document.getElementById("repo-count").textContent = `${selected.length} / ${repos.length}`;
  document.getElementById("repo-sort").value = overviewSort;
  document.getElementById("row-trend-label").textContent = TREND_METRICS[overviewMetric].label;
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.setAttribute("aria-sort", th.dataset.sort === overviewSort ? (overviewDirection === "asc" ? "ascending" : "descending") : "none");
    th.querySelector("span").textContent = th.dataset.sort === overviewSort ? (overviewDirection === "asc" ? "↑" : "↓") : "";
  });
  document.getElementById("repo-rows").innerHTML = selected.length ? selected.map((r) => {
    const points = trends.repositories[r.name] || [];
    const observed = r.coverage?.traffic;
    const coverage = observed?.expected_days ? `${observed.observed_days}/${observed.expected_days} traffic days` : `${observed?.observed_days ?? 0} traffic days`;
    return `<tr><td><a class="repo-name" href="#/repo/${encodeURIComponent(r.name)}">${esc(r.name)} ↗</a>${r.archived ? ' <span class="badge">Archived</span>' : ""}
      <span class="repo-description">${esc(r.description || "No description")}</span><span class="repo-coverage">${esc(coverage)}</span></td>
      <td class="trend-cell">${sparkline(points.map((p) => p[overviewMetric]), css(TREND_METRICS[overviewMetric].color), `${r.name} ${TREND_METRICS[overviewMetric].label}`)}</td>
      ${Object.values(metrics).map((m) => {
        const v = m.get(r, r), delta = change(v, previousMetric(r, m), m.pct);
        const releaseTotal = m === METRICS[2] ? `<td class="r release-total"><span class="table-value">${fmt(r.downloads)}</span><span class="table-delta">${r.downloads_as_of ? `as of ${esc(shortDate(r.downloads_as_of))}` : "no snapshot"}</span></td>` : "";
        return `${releaseTotal}<td class="r"><span class="table-value">${fmt(v)}</span>${delta ? `<span class="table-delta ${delta.cls}">${esc(delta.txt)}</span>` : ""}</td>`;
      }).join("")}</tr>`;
  }).join("") : `<tr><td colspan="7"><div class="empty"><b>${repos.length ? "No repositories match" : "No data collected yet"}</b><span>${repos.length ? "Try another search or filter." : "Run repowatch collect to start building your history."}</span></div></td></tr>`;
}

function renderFeed() {
  const el = document.getElementById("feed");
  // Group by repo, groups ordered by their most recent event (events arrive newest first).
  const byRepo = new Map();
  for (const e of events) {
    const k = e.repo || "";
    if (!byRepo.has(k)) byRepo.set(k, []);
    byRepo.get(k).push(e);
  }
  let budget = showAllEvents ? Infinity : FEED_LIMIT;
  const groups = [];
  for (const [repo, list] of byRepo) {
    if (budget <= 0) break;
    const shown = list.slice(0, budget);
    budget -= shown.length;
    groups.push({ repo, list: shown, total: list.length });
  }
  const body = groups.map((g) => {
    const multi = g.list.length > 1 && g.repo;
    const head = multi ? `<div class="group-head"><a href="#/repo/${encodeURIComponent(g.repo)}">${esc(g.repo)}</a>
      <span class="meta">${g.total} events</span></div>` : "";
    return `<div class="group">${head}${g.list.map((e) => eventRow(e, { showRepo: !multi, dismiss: true })).join("")}</div>`;
  }).join("");

  el.innerHTML = `
    <div class="card-head">
      <div><span class="card-title">What's new</span>${events.length ? `<span class="count">${events.length}</span>` : ""}</div>
      <div class="actions">
        ${events.length ? '<button class="link-btn" id="seen-all">Mark all seen</button>' : ""}
        <a href="#/events">History</a>
      </div>
    </div>
    ${events.length ? `<div class="feed">${body}</div>`
      : '<div class="empty"><b>Nothing new since your last visit</b><span>Past events stay in <a href="#/events">History</a>.</span></div>'}
    ${events.length > FEED_LIMIT ? `<button class="more" id="more">${showAllEvents ? "Show fewer" : `Show all (${events.length})`}</button>` : ""}`;

  el.querySelectorAll(".seen").forEach((b) =>
    b.addEventListener("click", async () => {
      const id = Number(b.dataset.id);
      await markSeen({ ids: [id] });
      events = events.filter((e) => e.id !== id);
      renderFeed();
    }));
  document.getElementById("seen-all")?.addEventListener("click", async () => {
    await markSeen({ all: true });
    events = [];
    renderFeed();
  });
  document.getElementById("more")?.addEventListener("click", () => {
    showAllEvents = !showAllEvents;
    renderFeed();
  });
}

async function renderEvents(version) {
  const all = await api("events?include_seen=true&limit=500");
  if (version !== renderVersion) return;
  clearCharts();
  view.innerHTML = `<a class="back" href="#/">← Overview</a>
    <section class="card">
      <div class="card-head"><span class="card-title">History</span><span class="meta">${all.length} events</span></div>
      ${all.length ? `<div class="feed">${all.map((e) => `<div class="group">${eventRow(e, { showRepo: true, dismiss: false })}</div>`).join("")}</div>`
        : '<div class="empty"><b>No events yet</b><span>They appear after the next collection run.</span></div>'}
    </section>`;
}

// ------------------------------------------------------------- repo detail

async function renderRepo(name, version) {
  const d = await api(`repo/${encodeURIComponent(name)}?days=${days}`);
  if (version !== renderVersion) return;
  clearCharts();
  const r = d.repo;
  const p = d.period;
  const prev = d.prev ? { ...d.prev } : null;
  if (prev && !p.coverage?.pypi?.complete) prev.pypi_downloads = null;
  if (prev && !p.coverage?.traffic?.complete) {
    prev.views_uniques = null;
    prev.clones = null;
  }
  const t = d.traffic;
  const snap = d.snapshots[d.snapshots.length - 1] || {};
  const stars = starSeries(d.stars);
  const latestStars = snap.stars ?? stars[stars.length - 1]?.stars;
  const totalDl = d.releases.reduce((a, x) => a + x.downloads, 0);
  const releaseTotal = d.release_downloads;
  // Keys are "tag · asset": the tag is noise when every asset comes from the same release.
  const assets = Object.entries(d.downloads)
    .filter(([, pts]) => pts.length)
    .map(([label, pts]) => ({ label, count: pts[pts.length - 1].count }));
  if (new Set(assets.map((a) => a.label.split(" · ")[0])).size === 1) {
    assets.forEach((a) => { a.label = a.label.split(" · ").slice(1).join(" · "); });
  }
  const pypiTotal = d.pypi.reduce((a, x) => a + x.downloads, 0);
  const cmp = compareLabel();

  const extra = [
    `${fmt(snap.forks)} forks`, `${fmt(snap.watchers)} watchers`,
    d.releases.length ? `${fmt(totalDl)} release downloads all time` : "",
  ].filter(Boolean).join(" · ");

  view.innerHTML = `
    <a class="back" href="#/">← Overview</a>
    <header class="detail-head">
      <div><h1>${esc(r.name)} ${r.archived ? '<span class="badge">Archived</span>' : ""}</h1>
        <div class="sub">${esc(r.description || "")}</div></div>
      <a class="gh" href="${esc(r.html_url)}" target="_blank" rel="noopener">${mobile.matches ? "GitHub ↗" : "View on GitHub ↗"}</a>
    </header>

    <div class="kpi-block">
      <section class="kpis">
        ${kpiTile("Stars", latestStars, p.stars_delta ? { txt: `${p.stars_delta > 0 ? "+" : ""}${fmt(p.stars_delta)}`, cls: p.stars_delta > 0 ? "pos" : "" } : null, days ? `in ${periodShort()}` : "all time")}
        ${kpiTile("Daily unique visitors (sum)", p.views_uniques, change(p.views_uniques, prev?.views_uniques, true), cmp, METRICS[0].hint)}
        ${kpiTile("Release downloads (total)", releaseTotal.total, null,
          releaseTotal.as_of ? `as of ${shortDate(releaseTotal.as_of)}${releaseTotal.as_of === today() ? " · includes today" : ""}` : "no snapshot",
          "Cumulative GitHub release-asset downloads across all releases. Independent of the selected period. PyPI is shown separately.",
          `<p class="kpi-period">${fmt(p.downloads_delta)} observed increase ${esc(days ? `in ${periodShort()}` : "since tracking began")} · through yesterday</p>`)}
        ${kpiTile("Clones", p.clones, change(p.clones, prev?.clones, true), days ? `${cmp} · incl. CI` : "incl. CI")}
      </section>
      <p class="meta">${esc(extra)}</p>
    </div>

    <p class="meta">Traffic coverage: ${fmt(p.coverage?.traffic?.observed_days)}${p.coverage?.traffic?.expected_days ? ` / ${fmt(p.coverage.traffic.expected_days)}` : ""} observed days · UTC · today excluded (partial). Gaps break the lines.</p>
    <section class="card chart-card main">
      <div class="chart-top"><span class="card-title">Visits</span>
        <div class="legend"><span><i></i>Unique visitors</span><span><i class="grey"></i>Views</span></div></div>
      ${t.length ? '<div class="chart-box"><canvas id="c-visits"></canvas></div>' : noData()}
    </section>

    <div class="grid2">
      <section class="card chart-card">
        <div class="chart-top"><span class="card-title">Clones vs CI checkouts</span>
          <div class="legend"><span><i class="box"></i>Clones</span><span><i class="box grey"></i>CI checkouts</span></div></div>
        <p class="note">CI checkouts are estimated from job step names. The difference does not measure human clones. Missing CI days mean no observation, not zero.</p>
        ${t.length ? '<div class="chart-box"><canvas id="c-clones"></canvas></div>' : noData()}
      </section>
      <section class="card chart-card">
        <span class="card-title">Stars, observed total</span>
        ${stars.length ? `<p class="note tab-nums">${fmt(stars[0].stars)} → ${fmt(stars[stars.length - 1].stars)}</p>
          <div class="chart-box"><canvas id="c-stars"></canvas></div>` : noData()}
      </section>
      <section class="card chart-card">
        <span class="card-title">Downloads by asset</span>
        ${assets.length ? `<p class="note">Cumulative per asset · last observation in selected period · may include multiple releases</p>
          <div class="chart-box" style="height:${Math.max(120, assets.length * 30)}px"><canvas id="c-assets"></canvas></div>`
          : noData(d.releases.length ? "No asset downloads in this period." : "No releases yet. Counts appear once a release has assets.")}
      </section>
      ${d.pypi.length ? `<section class="card chart-card">
        <span class="card-title">PyPI downloads</span>
        <p class="note tab-nums">${fmt(pypiTotal)} in ${esc(periodShort())}</p>
        <div class="chart-box"><canvas id="c-pypi"></canvas></div>
      </section>` : ""}
    </div>

    <section class="card" id="tables"></section>`;

  renderTables(d);

  const accent = css("--accent");
  const grey = css("--fg-3");
  const day = (iso) => shortDate(iso).replace(/,? \d{4}$/, "");
  const labels = t.map((x) => day(x.date));
  if (t.length) {
    chart(document.getElementById("c-visits"), "line", labels, [
      { label: "Unique visitors", data: t.map((x) => x.views_uniques), borderColor: accent, backgroundColor: alpha(accent, 0.08), fill: true },
      { label: "Views", data: t.map((x) => x.views), borderColor: grey, borderWidth: 1.5 },
    ]);
    chart(document.getElementById("c-clones"), "bar", labels, [
      { label: "Clones", data: t.map((x) => x.clones), backgroundColor: accent, barPercentage: 0.9, categoryPercentage: 0.7 },
      { label: "CI checkouts", data: t.map((x) => x.ci_checkout_jobs), backgroundColor: alpha(grey, 0.6), barPercentage: 0.9, categoryPercentage: 0.7 },
    ]);
  }
  if (stars.length) {
    chart(document.getElementById("c-stars"), "line", stars.map((x) => day(x.date)), [
      { label: "Stars", data: stars.map((x) => x.stars), borderColor: accent, stepped: true, tension: 0 },
    ]);
  }
  if (assets.length) {
    chart(document.getElementById("c-assets"), "bar", assets.map((a) => a.label), [
      { label: "Downloads", data: assets.map((a) => a.count), backgroundColor: accent, barThickness: 14 },
    ], { horizontal: true });
  }
  if (d.pypi.length) {
    chart(document.getElementById("c-pypi"), "bar", d.pypi.map((x) => day(x.date)), [
      { label: "PyPI downloads", data: d.pypi.map((x) => x.downloads), backgroundColor: accent },
    ]);
  }
}

// Cumulative stars over the period: starts from the count at the cutoff and runs to today.
function starSeries(all) {
  if (!all.length) return [];
  const from = cutoff();
  const before = all.filter((x) => x.date < from).pop();
  const inside = all.filter((x) => x.date >= from && x.date < today());
  const pts = [...(before ? [{ date: from, stars: before.stars }] : []), ...inside];
  const last = pts[pts.length - 1];
  if (!last) return [];
  return pts;
}

const noData = (msg = "No data yet.") => `<p class="no-data">${esc(msg)}</p>`;

function table(head, rows) {
  if (!rows.length) return noData("Nothing here yet.");
  const cls = (i) => (i && head[i].r ? ' class="r"' : "");
  return `<div class="table-wrap"><table>
    <thead><tr>${head.map((h, i) => `<th${cls(i)}>${esc(h.t)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${row.map((c, i) => `<td${cls(i)}>${c}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

function renderTables(d) {
  const from = cutoff();
  const asOf = (dt) => (dt ? `<p class="meta">GitHub's last 14 days, as of ${esc(shortDate(dt))}</p>` : "");
  const link = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(text)}</a>`;
  const sections = [
    { key: "ref", label: "Referring sites", n: d.referrers.length,
      html: asOf(d.referrers_date) + table([{ t: "Site" }, { t: "Views", r: 1 }, { t: "Unique", r: 1 }],
        d.referrers.map((x) => [esc(x.referrer), fmt(x.count), fmt(x.uniques)])) },
    { key: "pages", label: "Popular pages", n: d.paths.length,
      html: asOf(d.paths_date) + table([{ t: "Path" }, { t: "Views", r: 1 }, { t: "Unique", r: 1 }],
        d.paths.map((x) => [link(`https://github.com${x.path}`, x.path), fmt(x.count), fmt(x.uniques)])) },
    { key: "all", label: "Every referrer seen", n: d.referrers_history.length,
      html: table([{ t: "Referrer" }, { t: "First seen" }, { t: "Peak 14-day snapshot (unique)", r: 1 }],
        d.referrers_history.map((x) => [
          `${esc(x.referrer)}${days && x.first_seen >= from ? ' <span class="badge new">New</span>' : ""}`,
          esc(shortDate(x.first_seen)), fmt(x.max_uniques)])) },
    { key: "rel", label: "Releases", n: d.releases.length,
      html: table([{ t: "Release" }, { t: "Published" }, { t: "Downloads", r: 1 }],
        d.releases.map((x) => [`${link(x.html_url, x.tag)}${x.prerelease ? ' <span class="badge">pre</span>' : ""}`,
          esc(shortDate(x.published_at)), fmt(x.downloads)])) },
    { key: "ext", label: "External activity", n: d.activity.length,
      html: table([{ t: "Item" }, { t: "By" }, { t: "When", r: 1 }],
        d.activity.map((x) => [`${glyph(x.kind)}${link(x.url, `${x.number ? `#${x.number} ` : ""}${x.title || x.kind}`)}`,
          esc(x.user ? `@${x.user}` : ""), esc(ago(x.created_at))])) },
  ];
  const el = document.getElementById("tables");
  if (mobile.matches) {
    el.innerHTML = sections.map((s) => `<details class="acc" name="tables">
      <summary>${esc(s.label)}<span class="n">${s.n}</span><span class="chev">▾</span></summary>
      <div class="panel">${s.html}</div></details>`).join("");
    return;
  }
  if (!sections.some((s) => s.key === activeTab)) activeTab = sections[0].key;
  const current = sections.find((s) => s.key === activeTab);
  el.innerHTML = `<div class="tabs" role="tablist">${sections.map((s) =>
    `<button role="tab" data-tab="${s.key}" class="${s.key === activeTab ? "active" : ""}" aria-selected="${s.key === activeTab}">${esc(s.label)}<span class="n">${s.n}</span></button>`).join("")}</div>
    <div class="panel" role="tabpanel">${current.html}</div>`;
  el.querySelectorAll(".tabs button").forEach((b) =>
    b.addEventListener("click", () => { activeTab = b.dataset.tab; renderTables(d); }));
}

// ------------------------------------------------------------- routing

async function render() {
  const version = ++renderVersion;
  renderedMobile = mobile.matches;
  clearCharts();
  view.innerHTML = '<div class="card loading-state" role="status"><b>Loading your projects…</b><p>Preparing observations for the selected period.</p></div>';
  document.querySelectorAll(".period button").forEach((b) => b.classList.toggle("active", Number(b.dataset.days) === days));
  const hash = location.hash;
  try {
    if (hash.startsWith("#/repo/")) await renderRepo(decodeURIComponent(hash.slice(7)), version);
    else if (hash === "#/events") await renderEvents(version);
    else await renderOverview(version);
  } catch (err) {
    if (version !== renderVersion) return;
    clearCharts();
    view.innerHTML = `<div class="card empty"><b>Could not load data</b><span>${esc(err.message)}</span></div>`;
  }
}

async function renderStatus() {
  try {
    const s = await api("status");
    document.getElementById("owner").textContent = s.owner || "";
    document.querySelector(".brand .slash").hidden = !s.owner;
    const run = s.last_run;
    document.getElementById("status").textContent = run
      ? `Last collected ${ago(run.finished_at || run.started_at)} · ${run.repos ?? "?"} repos · ${run.errors ?? 0} errors`
      : "Last collected — · 0 repos · 0 errors";
  } catch (_) { /* shown by render() */ }
}

document.querySelectorAll(".period button").forEach((b) =>
  b.addEventListener("click", () => {
    days = Number(b.dataset.days);
    try { localStorage.setItem("repowatch.days", String(days)); } catch (_) { /* ignore */ }
    render();
  }));
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
// Chart colours come from CSS variables and the tables switch between tabs and accordion.
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (!document.documentElement.dataset.theme) render(); });
mobile.addEventListener("change", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (mobile.matches !== renderedMobile) render(); }, 150);
});
const themeControl = document.getElementById("theme");
themeControl.value = document.documentElement.dataset.theme || "system";
themeControl.addEventListener("change", () => {
  const theme = themeControl.value;
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("repowatch.theme", theme); } catch (_) { /* ignore */ }
  render();
});
renderStatus();
render();
