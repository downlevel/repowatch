"use strict";

const view = document.getElementById("view");
const charts = [];
let days = 14;
try { days = Number(localStorage.getItem("repowatch.days") ?? 14); } catch (_) { /* storage blocked */ }

// ------------------------------------------------------------- helpers

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
const delta = (n) => (n ? `<span class="up">+${fmt(n)}</span>` : "");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const series = (i) => css(`--series-${(i % 8) + 1}`);
const periodLabel = () => (days ? `last ${days} days` : "all time");

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

function clearCharts() {
  while (charts.length) charts.pop().destroy();
}

// ------------------------------------------------------------- charts

function lineChart(canvas, labels, datasets, { stacked = false, type = "line" } = {}) {
  const grid = css("--grid");
  const text = css("--text-2");
  const chart = new Chart(canvas, {
    type,
    data: {
      labels,
      datasets: datasets.map((d, i) => ({
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        tension: 0.25,
        borderColor: d.color || series(i),
        backgroundColor: d.color || series(i),
        borderRadius: type === "bar" ? 4 : 0,
        maxBarThickness: 18,
        ...d,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: datasets.length > 1,
          position: "bottom",
          labels: { color: text, boxWidth: 12, boxHeight: 2, font: { size: 12 } },
        },
        tooltip: { callbacks: { footer: (items) => items[0]?.dataset.footer?.(items[0].dataIndex) ?? "" } },
      },
      scales: {
        x: { stacked, grid: { display: false }, ticks: { color: text, maxRotation: 0, autoSkipPadding: 16 } },
        y: { stacked, beginAtZero: true, grid: { color: grid }, border: { display: false }, ticks: { color: text, precision: 0 } },
      },
    },
  });
  charts.push(chart);
  return chart;
}

// ------------------------------------------------------------- overview

async function renderOverview() {
  const [events, repos] = await Promise.all([api("events?limit=100"), api(`summary?days=${days}`)]);
  clearCharts();
  view.innerHTML = `
    <section class="card">
      <div class="feed-head">
        <h3>What's new <span class="muted small">(${events.length} unseen)</span></h3>
        <div>
          <button class="btn" id="all-events">History</button>
          ${events.length ? '<button class="btn" id="seen-all">Mark all seen</button>' : ""}
        </div>
      </div>
      ${events.length ? `<ul class="feed">${events.map((e) => eventRow(e)).join("")}</ul>`
                      : '<p class="empty">Nothing new since you last looked.</p>'}
    </section>

    <h2>Repositories <span class="muted small">· ${periodLabel()}</span></h2>
    <section class="card table-wrap">
      <table class="summary">
        <thead><tr>
          <th>Repository</th><th class="num">Stars</th><th class="num">Downloads</th>
          <th class="num">Unique visitors*</th><th class="num">Clones / CI runs</th>
          <th>Top referrer (14d)</th><th>Latest release</th><th class="num">External activity</th><th>Last push</th>
        </tr></thead>
        <tbody>${repos.map(repoRow).join("") || '<tr><td colspan="9" class="empty">No data yet: run <code>repowatch collect</code>.</td></tr>'}</tbody>
      </table>
    </section>
    <p class="note">* Sum of daily unique visitors: a person visiting on two days counts twice.
      Clones include GitHub Actions checkouts, so compare them with CI runs before reading them as users.</p>`;

  view.querySelectorAll(".feed .x").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("events/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [Number(b.dataset.id)] }) });
      b.closest("li").remove();
    }));
  document.getElementById("seen-all")?.addEventListener("click", async () => {
    await api("events/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: true }) });
    render();
  });
  document.getElementById("all-events").addEventListener("click", () => { location.hash = "#/events"; });
}

function eventRow(e, withDismiss = true) {
  const what = e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.detail)}</a>` : esc(e.detail);
  return `<li><span class="when">${esc(ago(e.date))}</span><span class="what">${what}</span>
    ${withDismiss && !e.seen ? `<button class="x" data-id="${e.id}" title="Mark as seen" aria-label="Mark as seen">×</button>` : ""}</li>`;
}

function repoRow(r) {
  const rel = r.latest_release;
  const ref = r.top_referrer;
  const pypi = r.pypi_downloads !== null ? `<span class="sub">PyPI ${fmt(r.pypi_downloads)}</span>` : "";
  return `<tr>
    <td><a class="repo-name" href="#/repo/${encodeURIComponent(r.name)}">${esc(r.name)}</a>
        ${r.archived ? '<span class="badge">archived</span>' : ""}
        <span class="sub">${esc(r.description || "")}</span></td>
    <td class="num" data-label="Stars">${fmt(r.stars)}${delta(r.stars_delta)}</td>
    <td class="num" data-label="Downloads">${fmt(r.downloads)}${delta(r.downloads_delta)}${pypi}</td>
    <td class="num" data-label="Unique visitors">${fmt(r.views_uniques)}<span class="sub">${fmt(r.views)} views</span></td>
    <td class="num" data-label="Clones / CI runs">${fmt(r.clones)} / ${fmt(r.ci_runs)}</td>
    <td data-label="Top referrer">${ref ? `${esc(ref.referrer)} <span class="sub">${fmt(ref.uniques)} unique</span>` : '<span class="muted">—</span>'}</td>
    <td data-label="Latest release">${rel ? `<a href="${esc(rel.html_url)}" target="_blank" rel="noopener">${esc(rel.tag)}</a><span class="sub">${esc(ago(rel.published_at))}</span>` : '<span class="muted">—</span>'}</td>
    <td class="num" data-label="External activity">${fmt(r.external_activity)}</td>
    <td data-label="Last push">${esc(ago(r.pushed_at))}</td>
  </tr>`;
}

async function renderEvents() {
  const events = await api("events?include_seen=true&limit=500");
  clearCharts();
  view.innerHTML = `<p><a href="#/">← All repositories</a></p>
    <section class="card"><h3>All events</h3>
    ${events.length ? `<ul class="feed">${events.map((e) => eventRow(e, false)).join("")}</ul>` : '<p class="empty">No events yet.</p>'}
    </section>`;
}

// ------------------------------------------------------------- repo detail

async function renderRepo(name) {
  const d = await api(`repo/${encodeURIComponent(name)}?days=${days}`);
  clearCharts();
  const r = d.repo;
  const t = d.traffic;
  const sum = (k) => t.reduce((a, x) => a + x[k], 0);
  const last = d.snapshots[d.snapshots.length - 1] || {};
  const totalDl = d.releases.reduce((a, x) => a + x.downloads, 0);

  view.innerHTML = `
    <p><a href="#/">← All repositories</a></p>
    <h2 style="margin-top:0"><a href="${esc(r.html_url)}" target="_blank" rel="noopener">${esc(r.name)}</a>
      <span class="muted small">${esc(r.description || "")}</span></h2>
    <div class="stats">
      ${stat(fmt(last.stars), "stars")}
      ${stat(fmt(last.forks), "forks")}
      ${stat(fmt(last.watchers), "watchers")}
      ${stat(fmt(totalDl), "release downloads")}
      ${stat(fmt(sum("views_uniques")), `daily uniques · ${periodLabel()}`)}
      ${stat(fmt(sum("clones")), `clones · ${periodLabel()}`)}
    </div>

    <div class="grid2" style="margin-top:14px">
      <section class="card"><h3>Visits</h3>${t.length ? '<div class="chart-box"><canvas id="c-views"></canvas></div>' : empty()}</section>
      <section class="card"><h3>Clones vs CI checkouts</h3>${t.length ? '<div class="chart-box"><canvas id="c-clones"></canvas></div>' : empty()}
        <p class="note">Every GitHub Actions job using <code>actions/checkout</code> is a clone. Clones well above the CI line are more likely to be people.</p></section>
      <section class="card"><h3>Stars (cumulative)</h3>${d.stars.length ? '<div class="chart-box"><canvas id="c-stars"></canvas></div>' : empty()}</section>
      <section class="card"><h3>Release downloads (cumulative per asset)</h3>${Object.keys(d.downloads).length ? '<div class="chart-box"><canvas id="c-dl"></canvas></div>' : empty("No release assets.")}</section>
      ${d.pypi.length ? '<section class="card"><h3>PyPI downloads per day</h3><div class="chart-box"><canvas id="c-pypi"></canvas></div></section>' : ""}
    </div>

    <div class="grid2" style="margin-top:14px">
      <section class="card"><h3>Referring sites <span class="muted small">(14d, as of ${esc(d.referrers_date || "—")})</span></h3>
        ${table(["Site", "Views", "Unique"], d.referrers.map((x) => [esc(x.referrer), fmt(x.count), fmt(x.uniques)]))}</section>
      <section class="card"><h3>Popular pages <span class="muted small">(14d, as of ${esc(d.paths_date || "—")})</span></h3>
        ${table(["Page", "Views", "Unique"], d.paths.map((x) => [`<a href="https://github.com${esc(x.path)}" target="_blank" rel="noopener">${esc(x.path)}</a>`, fmt(x.count), fmt(x.uniques)]))}</section>
      <section class="card"><h3>Every referrer seen</h3>
        ${table(["Site", "First seen", "Best day (unique)"], d.referrers_history.map((x) => [esc(x.referrer), esc(x.first_seen), fmt(x.max_uniques)]))}</section>
      <section class="card"><h3>Releases</h3>
        ${table(["Tag", "Published", "Downloads"], d.releases.map((x) => [`<a href="${esc(x.html_url)}" target="_blank" rel="noopener">${esc(x.tag)}</a>${x.prerelease ? '<span class="badge">pre</span>' : ""}`, esc((x.published_at || "").slice(0, 10)), fmt(x.downloads)]))}</section>
      <section class="card"><h3>External issues, PRs &amp; comments</h3>
        ${table(["", "By", "When"], d.activity.map((x) => [`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.kind)} #${esc(x.number)}</a> ${esc(x.title || "")}`, esc(x.user), esc(ago(x.created_at))]))}</section>
    </div>`;

  const labels = t.map((x) => x.date.slice(5));
  if (t.length) {
    lineChart(document.getElementById("c-views"), labels, [
      { label: "Views", data: t.map((x) => x.views) },
      { label: "Unique visitors", data: t.map((x) => x.views_uniques) },
    ]);
    lineChart(document.getElementById("c-clones"), labels, [
      { label: "Clones", data: t.map((x) => x.clones), footer: (i) => `CI runs: ${t[i].ci_runs}` },
      { label: "Unique cloners", data: t.map((x) => x.clones_uniques) },
      { label: "CI checkout jobs", data: t.map((x) => x.ci_checkout_jobs), borderDash: [4, 3], color: css("--muted") },
    ]);
  }
  if (d.stars.length) {
    const s = d.stars.concat([{ date: new Date().toISOString().slice(0, 10), stars: d.stars[d.stars.length - 1].stars }]);
    lineChart(document.getElementById("c-stars"), s.map((x) => x.date), [
      { label: "Stars", data: s.map((x) => x.stars), stepped: true, tension: 0 },
    ]);
  }
  const assets = Object.keys(d.downloads);
  if (assets.length) {
    const dates = [...new Set(assets.flatMap((a) => d.downloads[a].map((p) => p.date)))].sort();
    lineChart(document.getElementById("c-dl"), dates.map((x) => x.slice(5)), assets.map((a) => {
      const byDate = Object.fromEntries(d.downloads[a].map((p) => [p.date, p.count]));
      return { label: a, data: dates.map((x) => byDate[x] ?? null), spanGaps: true, pointRadius: dates.length < 3 ? 4 : 0 };
    }));
  }
  if (d.pypi.length) {
    lineChart(document.getElementById("c-pypi"), d.pypi.map((x) => x.date.slice(5)), [
      { label: "Downloads", data: d.pypi.map((x) => x.downloads) },
    ], { type: "bar" });
  }
}

const stat = (v, l) => `<div class="card stat"><div class="v">${v}</div><div class="l">${esc(l)}</div></div>`;
const empty = (msg = "No data yet.") => `<p class="empty">${esc(msg)}</p>`;

function table(head, rows) {
  if (!rows.length) return empty();
  return `<div class="table-wrap"><table><thead><tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
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
    view.innerHTML = `<p class="empty">Could not load data: ${esc(err.message)}</p>`;
  }
}

async function renderStatus() {
  try {
    const s = await api("status");
    document.getElementById("owner").textContent = s.owner ? `· ${s.owner}` : "";
    const run = s.last_run;
    document.getElementById("status").textContent = run
      ? `Last collected ${ago(run.finished_at || run.started_at)} · ${run.repos ?? "?"} repos${run.errors ? ` · ${run.errors} errors` : ""}`
      : "No collection run yet.";
  } catch (_) { /* shown by render() */ }
}

document.querySelectorAll(".period button").forEach((b) =>
  b.addEventListener("click", () => {
    days = Number(b.dataset.days);
    try { localStorage.setItem("repowatch.days", String(days)); } catch (_) { /* ignore */ }
    render();
  }));
window.addEventListener("hashchange", render);
renderStatus();
render();
