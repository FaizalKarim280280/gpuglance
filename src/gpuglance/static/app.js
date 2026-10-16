"use strict";
/* GPU Glance front end: fetches /api/snapshot and draws everything client-side. */

const $ = (s) => document.querySelector(s);
const POLL_MS = 5000; // cheap check; the server queries Slurm every 30s and we redraw only when it has new data
const BUCKETS = [ // busy GPUs, by how soon their job hits its time limit
  { cls: "s-b1", max: 3600, label: "< 1h" },
  { cls: "s-b2", max: 6 * 3600, label: "1–6h" },
  { cls: "s-b3", max: 24 * 3600, label: "6–24h" },
  { cls: "s-b4", max: Infinity, label: "> 1 day" },
];
const STATE_LABEL = {
  free: "Free", reserved: "Reserved", stranded: "Idle, but node has no CPU / memory left", down: "Down / draining",
};

const st = {
  snap: null, offset: 0, receivedAt: 0, error: null,
  model: null, q: "", freeOnly: false, tab: "R", showBlocked: false, range: 24, showAll: false,
  jobs: new Map(), models: new Map(),
};

// ------------------------------------------------------------------ utils
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const now = () => Date.now() / 1000 + st.offset;
const fmt = (n) => n.toLocaleString();

function dur(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m`;
  return "< 1m";
}
function clock(t) {
  const d = new Date((t - st.offset) * 1000), today = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (d.toDateString() === today.toDateString()) return hm;
  return d.toLocaleDateString([], { weekday: "short" }) + " " + hm;
}
function bucketOf(job) {
  if (!job || !job.end) return BUCKETS[1];
  const left = job.end - now();
  return BUCKETS.find((b) => left < b.max);
}
const slotCls = (state, job) => (state === "busy" ? (job ? bucketOf(job).cls : "s-busy") : "s-" + state);
const modelLabel = (k) => (st.models.get(k) || { label: k }).label;
const inModel = (m) => !st.model || m === st.model;

function matchesQuery(job, node) {
  if (!st.q) return true;
  const q = st.q;
  if (node && node.toLowerCase().includes(q)) return true;
  if (!job) return false;
  return job.user.toLowerCase().includes(q) || job.name.toLowerCase().includes(q) ||
    job.id.includes(q) || job.account.toLowerCase().includes(q) || job.nodes.some((n) => n.includes(q));
}

// ------------------------------------------------------------------ data
let inFlight = false;
async function load() {
  if (inFlight) return;
  inFlight = true;
  try { await fetchSnapshot(); } finally { inFlight = false; }
}

async function fetchSnapshot() {
  const fresh = $("#fresh");
  if (!st.snap) fresh.classList.add("loading");
  try {
    const t0 = Date.now();
    const r = await fetch("api/snapshot" + (st.snap ? "?v=" + st.snap.version : ""), { cache: "no-store" });
    const date = Date.parse(r.headers.get("Date"));
    if (!isNaN(date)) st.offset = (date - (t0 + Date.now()) / 2) / 1000;
    if (r.status === 204) { st.error = r.headers.get("X-Gpuglance-Error"); tickFresh(); return; }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    const snap = await r.json();
    st.snap = snap;
    st.error = null;
    st.jobs = new Map(snap.jobs.map((j) => [j.id, j]));
    st.models = new Map(snap.models.map((m) => [m.key, m]));
    if (st.model && !st.models.has(st.model)) st.model = null;
    render();
  } catch (e) {
    st.error = String(e.message || e);
    if (!st.snap) setTimeout(load, 3000);
  }
  fresh.classList.remove("loading");
  tickFresh();
}

function tickFresh() {
  const el = $("#fresh");
  if (!st.snap) { el.querySelector("span").textContent = st.error ? "error: " + st.error : "loading…"; return; }
  const age = now() - st.snap.generated_at;
  el.classList.toggle("stale", !!st.error || age > 180);
  el.querySelector("span").textContent = st.error ? `update failed · data ${dur(age)} old` : `updated ${age < 60 ? Math.round(age) + "s" : dur(age)} ago`;
  el.title = `Slurm query took ${st.snap.fetch_seconds ?? "?"}s · refreshes every ${st.snap.refresh_interval ?? 30}s`;
}

function render() {
  const s = st.snap;
  $("#cluster").textContent = s.cluster || "cluster";
  document.title = `${s.summary.total.free} free · ${s.cluster || "GPU"} Glance`;
  $("#me").textContent = s.user;
  renderHero(); renderMine(); renderChips(); renderCards(); renderMap(); renderChart(); renderJobs();
  $("#foot").textContent = `Partitions: ${s.partitions.join(", ")}. End times are time limits, so jobs often finish earlier. ` +
    `Queued = jobs waiting on Priority / Resources; a job that accepts several GPU types counts toward each.`;
}

// ------------------------------------------------------------------ pieces
function slotCounts(nodes) {
  const c = { free: 0, reserved: 0, stranded: 0, down: 0, "s-b1": 0, "s-b2": 0, "s-b3": 0, "s-b4": 0, "s-busy": 0 };
  for (const n of nodes) for (const [state, jid] of n.slots) {
    if (state === "busy") c[slotCls(state, st.jobs.get(jid))]++;
    else c[state]++;
  }
  return c;
}

function stateBar(nodes, thin) {
  const c = slotCounts(nodes);
  const total = nodes.reduce((a, n) => a + n.gpus, 0) || 1;
  const parts = [
    ["s-free", c.free, "Free"],
    ...BUCKETS.map((b) => [b.cls, c[b.cls], `Busy, frees in ${b.label}`]),
    ["s-busy", c["s-busy"], "Busy"],
    ["s-reserved", c.reserved, "Reserved"],
    ["s-stranded", c.stranded, "Idle, no CPU / memory left"],
    ["s-down", c.down, "Down / draining"],
  ].filter((p) => p[1] > 0);
  return `<div class="sbar${thin ? " thin" : ""}">` + parts.map(([cls, n, lab]) =>
    `<span class="${cls}" style="flex:${n / total}" data-tip="${esc(lab)}: ${n} GPU${n > 1 ? "s" : ""}"></span>`).join("") + `</div>`;
}

function legend() {
  return `<div class="legend">
    <span class="item"><span class="sw s-free"></span>Free</span>
    <span class="item"><span class="grp">${BUCKETS.map((b) => `<span class="sw ${b.cls}"></span>`).join("")}</span>Busy, frees in &lt;1h · 1–6h · 6–24h · &gt;1d</span>
    <span class="item"><span class="sw s-reserved"></span>Reserved</span>
    <span class="item"><span class="sw s-stranded"></span>Idle, node full</span>
    <span class="item"><span class="sw s-down"></span>Down / draining</span>
    <span class="item"><span class="sw" style="box-shadow:0 0 0 1.5px var(--surface),0 0 0 3px var(--ink)"></span>Yours</span>
  </div>`;
}

function renderHero() {
  const s = st.snap, t = s.summary.total;
  const unavailable = t.reserved + t.stranded + t.down;
  let soonest = null;
  for (const [k, m] of Object.entries(s.summary.models))
    if (!m.free && m.next_free && (!soonest || m.next_free < soonest[1])) soonest = [k, m.next_free];
  $("#hero").innerHTML = `
    <div class="hero-num"><span class="big">${fmt(t.free)}</span>
      <span class="lbl">GPUs free now<br><b>of ${fmt(t.total)}</b> on ${t.nodes} nodes</span></div>
    <div class="hero-stats">
      <div class="stat"><div class="v num">${fmt(t.busy)}</div><div class="k">in use</div></div>
      <div class="stat"><div class="v num">${fmt(t.queued_gpus)}</div><div class="k">GPUs queued (${fmt(t.queued_jobs)} jobs)</div></div>
      <div class="stat"><div class="v num">${fmt(unavailable)}</div><div class="k">reserved / unusable</div></div>
      ${soonest ? `<div class="stat"><div class="v">${dur(soonest[1] - now())}</div><div class="k">until next ${esc(modelLabel(soonest[0]))} frees</div></div>` : ""}
    </div>
    <div class="wide">${stateBar(s.nodes)}${legend()}</div>`;
}

function ring(frac, cls, pending) {
  const r = 17, c = 2 * Math.PI * r;
  const f = Math.max(0, Math.min(1, frac));
  return `<svg class="ring" width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
    <circle cx="22" cy="22" r="${r}" fill="none" stroke="var(--grid)" stroke-width="5"/>
    ${pending ? "" : `<circle cx="22" cy="22" r="${r}" fill="none" stroke="var(--${cls.slice(2)})" stroke-width="5"
      stroke-dasharray="${c * f} ${c}" stroke-linecap="round" transform="rotate(-90 22 22)"/>`}</svg>`;
}

function renderMine() {
  const mine = st.snap.jobs.filter((j) => j.mine)
    .sort((a, b) => (a.state === "R" ? 0 : 1) - (b.state === "R" ? 0 : 1) || (a.end || a.est_start || 9e12) - (b.end || b.est_start || 9e12));
  const el = $("#mine");
  el.hidden = !mine.length;
  if (!mine.length) return;
  el.innerHTML = `<div class="lab">Your jobs</div>` + mine.map((j) => {
    const lab = `${j.gpus}× ${esc(modelLabel(j.models[0]))}`;
    if (j.state === "R") {
      const b = bucketOf(j), frac = j.limit ? (now() - j.start) / j.limit : 0;
      return `<div class="myjob" data-job="${esc(j.id)}">${ring(1 - frac, b.cls)}
        <div><div class="t1">${esc(j.name)}</div>
        <div class="t3">${dur(j.end - now())} <small>left · ends ${clock(j.end)}</small></div>
        <div class="t2">${lab} · ${esc(j.nodes.join(", "))}</div></div></div>`;
    }
    return `<div class="myjob" data-job="${esc(j.id)}">${ring(0, "", true)}
      <div><div class="t1">${esc(j.name)}</div>
      <div class="t3">${j.est_start ? "~" + dur(j.est_start - now()) : "queued"} <small>${j.est_start ? "est. start " + clock(j.est_start) : esc(j.reason)}</small></div>
      <div class="t2">${lab}${j.tasks > 1 ? ` · ${j.tasks} tasks` : ""} · waiting ${dur(now() - j.submit)}</div></div></div>`;
  }).join("");
}

function renderChips() {
  const s = st.snap;
  const chip = (key, label, free) => `<button class="chip${st.model === key ? " on" : ""}" data-model="${esc(key ?? "")}" role="tab">
    ${key ? `<span class="dot${free ? "" : " none"}"></span>` : ""}${esc(label)}${key ? ` <span class="num">${free}</span>` : ""}</button>`;
  $("#chips").innerHTML = chip(null, "All types", s.summary.total.free) +
    s.models.map((m) => chip(m.key, m.label, s.summary.models[m.key].free)).join("");
}

function renderCards() {
  const s = st.snap;
  $("#cards").innerHTML = s.models.map((m) => {
    const x = s.summary.models[m.key];
    const nodes = s.nodes.filter((n) => n.model === m.key);
    const best = x.free_nodes[0];
    const next = x.free ? "now" : x.next_free ? "in " + dur(x.next_free - now()) : "–";
    const cls = st.model === m.key ? " on" : st.model ? " dim" : "";
    return `<div class="card${cls}" data-model="${esc(m.key)}" role="button" tabindex="0">
      <div class="hd"><span class="name">${esc(m.label)}</span><span class="mem">${esc(m.mem)}${m.mem ? " · " : ""}${x.nodes} nodes</span></div>
      <div class="fig${x.free ? "" : " zero"}"><span class="v">${x.free}</span><span class="of">/ ${x.total} free</span></div>
      ${stateBar(nodes, true)}
      <dl>
        <dt>Best node</dt><dd>${best ? `<b>${best[1]}</b> free · ${esc(best[0])}` : "–"}</dd>
        <dt>Next frees</dt><dd><b>${next}</b></dd>
        <dt>Queued</dt><dd><b>${fmt(x.queued_gpus)}</b> GPUs</dd>
      </dl></div>`;
  }).join("");
}

function renderMap() {
  const s = st.snap;
  const groups = s.models.filter((m) => inModel(m.key));
  let shown = 0;
  const html = groups.map((m) => {
    let nodes = s.nodes.filter((n) => n.model === m.key);
    if (st.freeOnly) nodes = nodes.filter((n) => n.slots.some((x) => x[0] === "free"));
    if (st.q) nodes = nodes.filter((n) => n.name.includes(st.q) || n.slots.some(([, j]) => matchesQuery(st.jobs.get(j))));
    if (!nodes.length) return "";
    shown += nodes.length;
    const x = s.summary.models[m.key];
    const tiles = nodes.map((n) => {
      const cols = n.gpus <= 8 ? 4 : 8;
      const slots = n.slots.map(([state, jid], i) => {
        const job = jid ? st.jobs.get(jid) : null;
        const dim = st.q && !n.name.includes(st.q) && !matchesQuery(job) ? " dimq" : "";
        return `<span class="slot ${slotCls(state, job)}${job && job.mine ? " mine" : ""}${dim}" data-node="${n.name}" data-i="${i}"${jid ? ` data-job="${esc(jid)}"` : ""}></span>`;
      }).join("");
      const flags = [
        n.health !== "ok" ? `<i class="flag bad"></i>` : "",
        n.reservation ? `<i class="flag resv"></i>` : "",
        n.planned ? `<i class="flag planned"></i>` : "",
      ].join("");
      return `<div class="node ${n.health}" data-node="${n.name}">
        ${flags ? `<span class="flags">${flags}</span>` : ""}
        <div class="slots${n.gpus > 8 ? " small" : ""}" style="grid-template-columns:repeat(${cols},auto)">${slots}</div>
        <span class="cpubar"><i style="width:${n.cpus ? Math.min(100, (n.cpus_alloc / n.cpus) * 100).toFixed(1) : 0}%"></i></span>
        <span class="nname">${esc(n.name)}</span></div>`;
    }).join("");
    return `<div class="mgroup"><div class="glab"><b>${esc(m.label)}</b><span>${x.free} free · ${x.total} GPUs</span></div>
      <div class="tiles">${tiles}</div></div>`;
  }).join("");
  $("#map").innerHTML = html || `<div class="empty">No nodes match.</div>`;
  $("#mapNote").innerHTML = `<span style="display:inline-flex;gap:14px">
    <span><i class="flag planned" style="display:inline-block;vertical-align:-1px;border-color:var(--page)"></i> scheduler holding for a queued job</span>
    <span><i class="flag resv" style="display:inline-block;vertical-align:-1px;border-color:var(--page)"></i> reservation</span>
    <span><i class="flag bad" style="display:inline-block;vertical-align:-1px;border-color:var(--page)"></i> drain / down</span>
    <span><span class="cpubar" style="display:inline-block;width:26px;vertical-align:1px"><i style="width:65%"></i></span> CPUs allocated</span></span>`;
}

// ------------------------------------------------------------------ release chart
function releaseEvents() {
  const t0 = now();
  const ev = [];
  for (const j of st.snap.jobs) {
    if (j.state !== "R" || !j.end || !inModel(j.models[0])) continue;
    if (st.q && !matchesQuery(j)) continue;
    ev.push({ t: Math.max(j.end, t0), job: j });
  }
  return ev.sort((a, b) => a.t - b.t);
}

function niceStep(max, target) {
  const raw = max / target, p = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  return [1, 2, 5, 10].map((k) => k * p).find((s) => raw <= s) || p * 10;
}

function renderChart() {
  const box = $("#chart");
  const W = Math.max(320, box.clientWidth - 16), H = 230;
  const M = { l: 44, r: 20, t: 18, b: 28 };
  const t0 = now(), span = st.range * 3600, t1 = t0 + span;
  const all = releaseEvents();
  const ev = all.filter((e) => e.t <= t1);
  const pts = [[t0, 0]];
  let cum = 0;
  for (const e of ev) { cum += e.job.gpus; pts.push([e.t, cum]); }
  const scopeQueued = st.model ? st.snap.summary.models[st.model].queued_gpus : st.snap.summary.total.queued_gpus;
  const scopeFree = st.model ? st.snap.summary.models[st.model].free : st.snap.summary.total.free;
  const refFits = scopeQueued > 0 && scopeQueued <= Math.max(cum, 4) * 1.35;
  const yMax0 = Math.max(cum, refFits ? scopeQueued : 0, 4);
  const step = niceStep(yMax0, 4), yMax = Math.ceil(yMax0 / step) * step;
  const x = (t) => M.l + ((t - t0) / span) * (W - M.l - M.r);
  const y = (v) => H - M.b - (v / yMax) * (H - M.t - M.b);

  let d = `M${x(t0)},${y(0)}`;
  for (let i = 1; i < pts.length; i++) d += `H${x(pts[i][0]).toFixed(1)}V${y(pts[i][1]).toFixed(1)}`;
  d += `H${x(t1)}`;
  const area = d + `V${y(0)}H${x(t0)}Z`;

  let grid = "";
  for (let v = 0; v <= yMax; v += step)
    grid += `<line class="${v ? "gridl" : "base"}" x1="${M.l}" x2="${W - M.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${M.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  const hStep = st.range <= 6 ? 1 : st.range <= 24 ? 3 : 12;
  const first = new Date((t0 - st.offset) * 1000); first.setMinutes(0, 0, 0);
  for (let t = first.getTime() / 1000 + st.offset; t <= t1; t += 3600) {
    const local = new Date((t - st.offset) * 1000);
    if (t < t0 + span * 0.04 || local.getHours() % hStep) continue;
    const lab = local.getHours() === 0 ? local.toLocaleDateString([], { weekday: "short" }) : String(local.getHours()).padStart(2, "0") + ":00";
    grid += `<text x="${x(t)}" y="${H - 8}" text-anchor="middle">${lab}</text>`;
  }
  grid += `<text x="${x(t0)}" y="${H - 8}" text-anchor="start">now</text>`;
  const ref = refFits ? `<line class="ref" x1="${M.l}" x2="${W - M.r}" y1="${y(scopeQueued)}" y2="${y(scopeQueued)}"/>
    <text class="reflab" x="${W - M.r}" y="${y(scopeQueued) - 6}" text-anchor="end">${fmt(scopeQueued)} GPUs queued</text>` : "";
  const mineMarks = ev.filter((e) => e.job.mine).map((e) =>
    `<path class="minemark" d="M${x(e.t)},${H - M.b + 1}l-4.5,7h9z" data-job="${esc(e.job.id)}"/>`).join("");
  const last = pts[pts.length - 1];

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="GPUs released over time">
    ${grid}${ref}<path class="area" d="${area}"/><path class="ln" d="${d}"/>
    ${cum ? `<text x="${Math.min(x(last[0]) + 6, W - M.r - 4)}" y="${y(cum) - 8}" text-anchor="${x(last[0]) > W - 120 ? "end" : "start"}" style="fill:var(--ink);font-weight:600">${fmt(cum)}</text>` : ""}
    ${mineMarks}
    <line class="cross" id="cx" y1="${M.t}" y2="${H - M.b}" visibility="hidden"/>
    <circle class="dot" id="cdot" r="4.5" visibility="hidden"/>
    <rect id="chit" x="${M.l}" y="0" width="${W - M.l - M.r}" height="${H}" fill="transparent"/></svg>`;

  const scope = st.model ? modelLabel(st.model) : "all";
  $("#chartSub").textContent = `${scope === "all" ? "All" : scope} GPUs released by running jobs, at the latest (time limits). ` +
    `${scopeFree} free now${refFits ? "" : `, ${fmt(scopeQueued)} GPUs queued`}.` + (ev.some((e) => e.job.mine) ? " ▲ = your jobs end." : "");

  const hit = $("#chit"), cx = $("#cx"), dot = $("#cdot");
  hit.onmousemove = (e) => {
    const r = box.querySelector("svg").getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - M.l) / (W - M.l - M.r)) * span;
    let k = 0;
    while (k + 1 < pts.length && pts[k + 1][0] <= t) k++;
    const v = pts[k][1];
    cx.setAttribute("x1", px); cx.setAttribute("x2", px); cx.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", y(v)); dot.setAttribute("visibility", "visible");
    const near = ev.filter((q) => q.t <= t).slice(-4).reverse();
    showTip(e, `<div class="tt">${fmt(v)} GPUs released</div>
      <div class="ts">by ${clock(t)} · in ${dur(t - t0)}</div>
      ${near.length ? `<hr><div class="tm">Latest to end</div><div class="tj">${near.map((q) =>
        `<span class="sw ${bucketOf(q.job).cls}"></span><span class="x">${esc(q.job.user)} · ${q.job.gpus}× ${esc(modelLabel(q.job.models[0]))} · ${esc(q.job.nodes[0] || "")}</span><span>${clock(q.t)}</span>`).join("")}</div>` : ""}`);
  };
  hit.onmouseleave = () => { cx.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); };
}

// ------------------------------------------------------------------ jobs table
function renderJobs() {
  const s = st.snap;
  let jobs = s.jobs.filter((j) => j.models.some(inModel) && matchesQuery(j));
  const running = jobs.filter((j) => j.state === "R");
  const queued = jobs.filter((j) => j.state === "PD" && (st.showBlocked || j.waiting));
  $("#nR").textContent = running.length;
  $("#nPD").textContent = queued.length;
  $("#blockedWrap").hidden = st.tab !== "PD";
  const T = now();
  let rows;
  if (st.tab === "R") {
    rows = running.sort((a, b) => (b.mine - a.mine) || (a.end || 9e12) - (b.end || 9e12));
  } else {
    rows = queued.sort((a, b) => (b.mine - a.mine) || (b.waiting - a.waiting) || (a.est_start || 9e12) - (b.est_start || 9e12) || a.submit - b.submit);
  }
  const limit = st.showAll ? rows.length : 60;
  const head = st.tab === "R"
    ? `<div class="jrow head"><span>User</span><span>Job</span><span>GPUs</span><span>Nodes</span><span>Progress</span><span class="e">Ends in</span></div>`
    : `<div class="jrow head"><span>User</span><span>Job</span><span>GPUs</span><span>Waiting</span><span>Reason</span><span class="e">Est. start</span></div>`;
  const body = rows.slice(0, limit).map((j) => {
    const g = `${j.gpus}× ${j.models.length > 2 ? "any of " + j.models.length : esc(j.models.map(modelLabel).join(" / "))}`;
    if (j.state === "R") {
      const b = bucketOf(j);
      const frac = j.limit ? Math.min(1, (T - j.start) / j.limit) : 0;
      return `<div class="jrow${j.mine ? " mine" : ""}" data-job="${esc(j.id)}">
        <span class="u">${esc(j.user)}</span>
        <span class="n">${esc(j.name)}<small>${esc(j.id)}</small></span>
        <span class="g"><span class="sw ${b.cls}"></span>${g}</span>
        <span class="w">${esc(j.nodes.length > 2 ? j.nodes.slice(0, 2).join(", ") + ` +${j.nodes.length - 2}` : j.nodes.join(", "))}</span>
        <span class="p"><div class="prog"><i class="${b.cls}" style="width:${(frac * 100).toFixed(1)}%"></i></div></span>
        <span class="e">${dur(j.end - T)}</span></div>`;
    }
    const gq = j.tasks > 1 ? `${g} <span class="tm">×${j.tasks}</span>` : g;
    return `<div class="jrow${j.mine ? " mine" : ""}${j.waiting ? "" : " blocked"}" data-job="${esc(j.id)}">
      <span class="u">${esc(j.user)}</span>
      <span class="n">${esc(j.name)}<small>${esc(j.id)}</small></span>
      <span class="g">${gq}</span>
      <span class="w">${dur(T - j.submit)}</span>
      <span class="p"><span class="reason">${esc(j.reason)}</span></span>
      <span class="e">${j.est_start ? (j.est_start <= T + 60 ? "now" : "~" + dur(j.est_start - T)) : "–"}</span></div>`;
  }).join("");
  const more = rows.length > limit ? `<button class="more" id="more">Show all ${rows.length}</button>` : "";
  $("#jobs").innerHTML = rows.length ? head + body + more : `<div class="empty" style="padding:16px">No jobs match.</div>`;
}

// ------------------------------------------------------------------ tooltips & highlight
const tip = $("#tip");
function showTip(e, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14, r = tip.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = Math.max(8, e.clientY - r.height - pad);
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
function hideTip() { tip.hidden = true; }

function jobTip(j) {
  const T = now();
  const where = Object.entries(j.alloc || {}).map(([n, idx]) => `${n} [${idx.join(",")}]`).join(", ") || j.nodes.join(", ");
  const lines = [
    `<div class="tt">${esc(j.name)}</div>`,
    `<div class="ts">${esc(j.user)} · ${esc(j.id)}${j.mine ? " · yours" : ""}</div><hr>`,
  ];
  const row = (k, v) => `<div class="tr"><span class="tm">${k}</span><span>${v}</span></div>`;
  lines.push(row("GPUs", `${j.gpus}× ${esc(j.models.map(modelLabel).join(" / "))}${j.tasks > 1 ? ` · ${j.tasks} tasks` : ""}`));
  if (j.state === "R") {
    const frac = j.limit ? Math.min(1, (T - j.start) / j.limit) : 0;
    lines.push(row("Nodes", esc(where)));
    lines.push(row("Ends in", `<b>${dur(j.end - T)}</b> · ${clock(j.end)}`));
    lines.push(`<div class="mbar"><i class="${bucketOf(j).cls}" style="width:${frac * 100}%"></i></div>`);
    lines.push(row("Running", `${dur(T - j.start)} of ${dur(j.limit)}`));
  } else {
    lines.push(row("Reason", esc(j.reason)));
    lines.push(row("Est. start", j.est_start ? `~${dur(j.est_start - T)} · ${clock(j.est_start)}` : "unknown"));
    lines.push(row("Waiting", dur(T - j.submit)));
    lines.push(row("Partitions", esc(j.partitions.join(", "))));
  }
  lines.push(row("CPU / mem", `${j.cpus} · ${esc(j.mem || "–")}`));
  lines.push(row("Account", `${esc(j.account)} · ${esc(j.qos)}`));
  if (j.mine) lines.push(`<hr><div class="tm">Click for details: usage, processes, log</div>`);
  return lines.join("");
}

function nodeTip(n, slotIdx) {
  const T = now();
  const m = st.models.get(n.model) || { label: n.model };
  const free = n.slots.filter((x) => x[0] === "free").length;
  const cpuF = n.cpus ? n.cpus_alloc / n.cpus : 0, memF = n.mem_mb ? n.mem_alloc_mb / n.mem_mb : 0;
  const gb = (mb) => Math.round(mb / 1024);
  const jobs = [...new Set(n.slots.map((x) => x[1]).filter(Boolean))].map((id) => st.jobs.get(id)).filter(Boolean)
    .sort((a, b) => (a.end || 0) - (b.end || 0));
  let h = `<div class="tt">${esc(n.name)} · ${n.gpus}× ${esc(m.label)}</div>
    <div class="ts">${free} free · ${esc(n.states.join(" + ").toLowerCase())}</div>`;
  if (slotIdx != null) {
    const [state] = n.slots[slotIdx];
    if (state !== "busy") h += `<div class="ts">GPU ${slotIdx}: ${esc(STATE_LABEL[state] || state)}</div>`;
  }
  if (n.reason) h += `<div class="ts" style="color:var(--down-ink)">${esc(n.reason)}</div>`;
  if (n.reservation) h += `<div class="ts">Reservation ${esc(n.reservation)}${n.reservation_end ? " · ends " + clock(n.reservation_end) : ""}${n.reserved_for_me ? " (includes you)" : ""}</div>`;
  if (n.planned) h += `<div class="ts">Scheduler is holding this node for a queued job</div>`;
  h += `<hr><div class="tr"><span class="tm">CPU</span><span>${n.cpus_alloc} / ${n.cpus} allocated</span></div><div class="mbar"><i style="width:${cpuF * 100}%"></i></div>
    <div class="tr"><span class="tm">Memory</span><span>${gb(n.mem_alloc_mb)} / ${gb(n.mem_mb)} GB allocated</span></div><div class="mbar"><i style="width:${memF * 100}%"></i></div>`;
  if (jobs.length) {
    h += `<div class="tj">` + jobs.map((j) => {
      const k = n.slots.filter((x) => x[1] === j.id).length;
      return `<span class="sw ${bucketOf(j).cls}"></span><span class="x">${j.mine ? "<b>" : ""}${esc(j.user)}${j.mine ? "</b>" : ""} · ${k} GPU${k > 1 ? "s" : ""} · ${esc(j.name)}</span><span>${dur(j.end - T)}</span>`;
    }).join("") + `</div>`;
  }
  return h;
}

function highlight(jid) {
  const map = $("#map");
  map.querySelectorAll(".slot.on").forEach((e) => e.classList.remove("on"));
  if (!jid) { map.classList.remove("hl"); return; }
  const els = map.querySelectorAll(`.slot[data-job="${CSS.escape(jid)}"]`);
  if (!els.length) { map.classList.remove("hl"); return; }
  els.forEach((e) => e.classList.add("on"));
  map.classList.add("hl");
}

let hovered = null;
document.addEventListener("mousemove", (e) => {
  if (e.target.id === "chit") return;
  const slot = e.target.closest(".slot");
  const jobEl = slot ? null : e.target.closest("[data-job]");
  const nodeEl = slot ? null : e.target.closest(".node");
  const tipEl = e.target.closest("[data-tip]");
  let key = null, html = null, hl = null;
  if (slot) {
    const n = st.snap.nodes.find((x) => x.name === slot.dataset.node);
    const job = slot.dataset.job && st.jobs.get(slot.dataset.job);
    key = "s" + slot.dataset.node + slot.dataset.i;
    html = job ? jobTip(job) + `<hr><div class="tm">${esc(n.name)} · GPU ${slot.dataset.i}</div>` : nodeTip(n, +slot.dataset.i);
    hl = job ? job.id : null;
  } else if (jobEl) {
    const job = st.jobs.get(jobEl.dataset.job);
    if (job) { key = "j" + job.id; html = jobTip(job); hl = job.id; }
  } else if (nodeEl) {
    const n = st.snap.nodes.find((x) => x.name === nodeEl.dataset.node);
    key = "n" + n.name; html = nodeTip(n);
  } else if (tipEl) {
    key = "t" + tipEl.dataset.tip; html = esc(tipEl.dataset.tip);
  }
  if (!html) { if (hovered) { hideTip(); highlight(null); hovered = null; } return; }
  if (key !== hovered) { highlight(hl); hovered = key; }
  showTip(e, html);
});
document.addEventListener("mouseleave", () => { hideTip(); highlight(null); hovered = null; });

// ------------------------------------------------------------------ controls
function setModel(k) { st.model = k && k !== st.model ? k : null; st.showAll = false; render(); }
$("#chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) setModel(b.dataset.model || null); });
$("#cards").addEventListener("click", (e) => { const c = e.target.closest(".card"); if (c) setModel(c.dataset.model); });
$("#cards").addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target.dataset.model) { e.preventDefault(); setModel(e.target.dataset.model); } });
$("#freeOnly").addEventListener("change", (e) => { st.freeOnly = e.target.checked; renderMap(); });
let qTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => { st.q = e.target.value.trim().toLowerCase(); st.showAll = false; renderMap(); renderChart(); renderJobs(); }, 120);
});
$("#range").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  st.range = +b.dataset.h;
  $("#range").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderChart();
});
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  st.tab = b.dataset.tab; st.showAll = false;
  $("#tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderJobs();
});
$("#showBlocked").addEventListener("change", (e) => { st.showBlocked = e.target.checked; renderJobs(); });
$("#jobs").addEventListener("click", (e) => { if (e.target.id === "more") { st.showAll = true; renderJobs(); } });

const savedTheme = store.get("gpuglance-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  store.set("gpuglance-theme", document.documentElement.dataset.theme);
});

new IntersectionObserver(([e]) => $("#toolbar").classList.toggle("stuck", e.intersectionRatio < 1), { threshold: [1], rootMargin: "-1px 0px 0px 0px" })
  .observe($("#toolbar"));
let rTimer;
addEventListener("resize", () => { clearTimeout(rTimer); rTimer = setTimeout(() => st.snap && renderChart(), 150); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

load();
setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
setInterval(tickFresh, 1000);
