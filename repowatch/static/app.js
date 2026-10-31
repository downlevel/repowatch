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

// ------------------------------------------------------------- helpers

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const periodShort = () => (days ? `${days}d` : "all time");
const periodLabel = () => (days ? `last ${days} days` : "all time");
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
  { label: "Unique visitors", short: "Visitors", pct: true, get: (p) => p?.views_uniques,
    hint: "Sum of daily unique visitors: a person visiting on two days counts twice." },
  { label: "New stars", short: "New stars", get: (p) => p?.stars_delta },
  { label: "Downloads", short: "Downloads", pct: true, get: (p, ref) => newDownloads(p, ref),
    hint: "New release-asset downloads plus PyPI downloads in the period." },
];
const EXTERNAL_METRIC = { label: "External activity", get: (p) => p?.external_activity,
  hint: "Issues, pull requests and comments from people other than you (bots excluded)." };

// Totals over all repos; the change only uses repos that also have data for the previous window.
function total(repos, m) {
  let value = null, curCmp = 0, prevCmp = 0, comparable = false;
  for (const r of repos) {
    const v = m.get(r, r);
    if (v === null || v === undefined) continue;
    value = (value ?? 0) + v;
    const p = r.prev ? m.get(r.prev, r) : null;
    if (p !== null && p !== undefined) { curCmp += v; prevCmp += p; comparable = true; }
  }
  return { value, d: comparable ? change(curCmp, prevCmp, m.pct) : null };
}

const activity = (r) => (r.views_uniques || 0) + (r.stars_delta || 0) + (newDownloads(r) || 0) + (r.external_activity || 0);
const isQuiet = (r) => !r.views_uniques && !r.stars_delta && !newDownloads(r) && !r.external_activity;

// ------------------------------------------------------------- charts

function chart(canvas, type, labels, datasets, { horizontal = false } = {}) {
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
      plugins: { legend: { display: false }, tooltip: { mode: "index", intersect: false } },
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
function kpiTile(label, value, d, caption, hint) {
  const cap = !d && days && caption.startsWith("vs prev") ? "no earlier data to compare" : caption;
  return `<div class="card kpi">
    <div class="l"${hint ? ` title="${esc(hint)}"` : ""}>${esc(label)}</div>
    <div class="v">${fmt(value)}</div>
    <div class="d">${deltaPill(d)}<span>${esc(cap)}</span></div>
  </div>`;
}

// ------------------------------------------------------------- overview

async function renderOverview() {
  const [evs, repos] = await Promise.all([api("events?limit=100"), api(`summary?days=${days}`)]);
  clearCharts();
  events = evs;
  const kpis = [...METRICS, EXTERNAL_METRIC].map((m) => {
    const t = total(repos, m);
    return kpiTile(m.label, t.value, t.d, compareLabel(), m.hint);
  });
  const active = repos.filter((r) => !isQuiet(r)).sort((a, b) => activity(b) - activity(a) || a.name.localeCompare(b.name));
  const quiet = repos.filter(isQuiet);

  view.innerHTML = `
    <section class="kpis">${kpis.join("")}</section>
    <div class="overview">
      <section class="card" id="feed"></section>
      <section class="card">
        <div class="card-head repo-head">
          <div><span class="card-title">Repositories</span><span class="meta">by activity · ${esc(periodLabel())}</span></div>
          <div class="cols">${METRICS.map((m) => `<span${m.hint ? ` title="${esc(m.hint)}"` : ""}>${esc(m.label)}</span>`).join("")}</div>
        </div>
        ${repos.length ? active.map(repoRow).join("") + quietGroup(quiet)
          : `<div class="empty"><b>No data collected yet</b>
             <span>Numbers appear after the first <code>repowatch collect</code> run. GitHub only keeps 14 days of traffic, so history starts from that run.</span></div>`}
      </section>
    </div>
    <p class="footnote">Visitors are summed daily uniques, so one person visiting on two days counts twice.</p>`;
  renderFeed();
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

function repoRow(r) {
  const cells = METRICS.map((m) => {
    const v = m.get(r, r);
    const d = r.prev ? change(v, m.get(r.prev, r), m.pct) : null;
    const sub = v === null || v === undefined
      ? `<span class="delta">${m.short === "Downloads" ? "no releases" : ""}</span>`
      : d ? `<span class="delta ${d.cls}">${esc(d.txt)}</span>` : "";
    return `<div class="metric"><span class="k">${esc(m.short)}</span><span class="v">${fmt(v)}</span>${sub}</div>`;
  }).join("");
  return `<a class="repo-row" href="#/repo/${encodeURIComponent(r.name)}">
    <div class="who">
      <div><span class="name">${esc(r.name)}</span> ${r.archived ? '<span class="badge">Archived</span>' : ""}</div>
      <div class="desc">${esc(r.description || "")}</div>
    </div>
    <div class="cols">${cells}</div>
  </a>`;
}

function quietGroup(quiet) {
  if (!quiet.length) return "";
  return `<details class="quiet">
    <summary><b>Quiet (${quiet.length})</b><span class="long">no visitors, stars, downloads or replies in ${esc(periodLabel())}</span><span class="short">no activity in ${esc(periodShort())}</span><span class="chev">▾</span></summary>
    <div class="quiet-list">${quiet.map((q) => `<div class="quiet-row">
      <a href="#/repo/${encodeURIComponent(q.name)}">${esc(q.name)}</a>${q.archived ? '<span class="badge">Archived</span>' : ""}
      <span class="desc">${esc(q.description || "")}</span></div>`).join("")}</div>
  </details>`;
}

async function renderEvents() {
  const all = await api("events?include_seen=true&limit=500");
  clearCharts();
  view.innerHTML = `<a class="back" href="#/">← Overview</a>
    <section class="card">
      <div class="card-head"><span class="card-title">History</span><span class="meta">${all.length} events</span></div>
      ${all.length ? `<div class="feed">${all.map((e) => `<div class="group">${eventRow(e, { showRepo: true, dismiss: false })}</div>`).join("")}</div>`
        : '<div class="empty"><b>No events yet</b><span>They appear after the next collection run.</span></div>'}
    </section>`;
}

// ------------------------------------------------------------- repo detail

async function renderRepo(name) {
  const d = await api(`repo/${encodeURIComponent(name)}?days=${days}`);
  clearCharts();
  const r = d.repo;
  const p = d.period;
  const prev = d.prev;
  const t = d.traffic;
  const snap = d.snapshots[d.snapshots.length - 1] || {};
  const stars = starSeries(d.stars);
  const latestStars = snap.stars ?? stars[stars.length - 1]?.stars;
  const totalDl = d.releases.reduce((a, x) => a + x.downloads, 0);
  const dl = newDownloads(p);
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
        ${kpiTile("Stars", latestStars, p.stars_delta ? { txt: `+${fmt(p.stars_delta)}`, cls: "pos" } : null, days ? `in ${periodShort()}` : "all time")}
        ${kpiTile("Unique visitors", p.views_uniques, change(p.views_uniques, prev?.views_uniques, true), cmp, METRICS[0].hint)}
        ${kpiTile("Downloads", dl, change(dl, newDownloads(prev, p), true), dl === null ? "no releases" : cmp, METRICS[2].hint)}
        ${kpiTile("Clones", p.clones, change(p.clones, prev?.clones, true), days ? `${cmp} · incl. CI` : "incl. CI")}
      </section>
      <p class="meta">${esc(extra)}</p>
    </div>

    <section class="card chart-card main">
      <div class="chart-top"><span class="card-title">Visits</span>
        <div class="legend"><span><i></i>Unique visitors</span><span><i class="grey"></i>Views</span></div></div>
      ${t.length ? '<div class="chart-box"><canvas id="c-visits"></canvas></div>' : noData()}
    </section>

    <div class="grid2">
      <section class="card chart-card">
        <div class="chart-top"><span class="card-title">Clones vs CI checkouts</span>
          <div class="legend"><span><i class="box"></i>Clones</span><span><i class="box grey"></i>CI checkouts</span></div></div>
        <p class="note">GitHub counts every Actions checkout as a clone; the gap is closer to real people.</p>
        ${t.length ? '<div class="chart-box"><canvas id="c-clones"></canvas></div>' : noData()}
      </section>
      <section class="card chart-card">
        <span class="card-title">Stars, cumulative</span>
        ${stars.length ? `<p class="note tab-nums">${fmt(stars[0].stars)} → ${fmt(stars[stars.length - 1].stars)}</p>
          <div class="chart-box"><canvas id="c-stars"></canvas></div>` : noData()}
      </section>
      <section class="card chart-card">
        <span class="card-title">Downloads by asset</span>
        ${assets.length ? `<p class="note">Total per asset · latest release ${esc(d.releases[0]?.tag || "—")}</p>
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
  const inside = all.filter((x) => x.date >= from);
  const pts = [...(before ? [{ date: from, stars: before.stars }] : []), ...inside];
  const last = pts[pts.length - 1];
  if (last.date < today()) pts.push({ date: today(), stars: last.stars });
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
      html: table([{ t: "Referrer" }, { t: "First seen" }, { t: "Best day (unique)", r: 1 }],
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
  document.querySelectorAll(".period button").forEach((b) => b.classList.toggle("active", Number(b.dataset.days) === days));
  const hash = location.hash;
  try {
    if (hash.startsWith("#/repo/")) await renderRepo(decodeURIComponent(hash.slice(7)));
    else if (hash === "#/events") await renderEvents();
    else await renderOverview();
  } catch (err) {
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
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
mobile.addEventListener("change", render);
renderStatus();
render();
