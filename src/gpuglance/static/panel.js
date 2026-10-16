"use strict";
/* Job details panel: opens when you click one of your own jobs. Uses helpers from app.js. */

const TOKEN = (() => {
  const u = new URL(location.href);
  const t = u.searchParams.get("token");
  if (t) {
    store.set("gpuglance-token", t);
    u.searchParams.delete("token"); // keep the secret out of the address bar / screenshots
    history.replaceState(null, "", u.pathname + (u.search || "") + u.hash);
    return t;
  }
  return store.get("gpuglance-token") || "";
})();

const DETAILS_MS = 15000; // details (sstat + log tail) refresh while the panel is open; ~0.1-0.3 s each
const pn = { id: null, d: null, live: null, liveBusy: false, err: null, timer: null, logTab: 0, scriptOpen: false };

class ApiError extends Error {
  constructor(status, body) { super(body.detail || body.error || "error " + status); this.status = status; }
}
async function api(path) {
  const r = await fetch(path, { cache: "no-store", headers: { "X-Gpuglance-Token": TOKEN } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(r.status, body);
  return body;
}

function fmtBytes(b) {
  if (b == null || !isFinite(b)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (b >= 100 || i === 0 ? Math.round(b) : b.toFixed(1)) + " " + u[i];
}
const meter = (frac, color) =>
  `<div class="meter"><i style="width:${(Math.max(0, Math.min(1, frac || 0)) * 100).toFixed(1)}%;background:var(${color})"></i></div>`;
const copyBtn = (text) => `<button class="copy" data-copy="${esc(text)}" title="Copy">copy</button>`;

// ------------------------------------------------------------------ open / close / load
function openPanel(id) {
  if (pn.id !== id) Object.assign(pn, { id, d: null, live: null, err: null, logTab: 0, scriptOpen: false });
  $("#scrim").hidden = false;
  $("#panel").hidden = false;
  document.body.classList.add("noscroll");
  hideTip();
  renderPanel();
  loadDetails();
  loadLive();
  clearInterval(pn.timer);
  pn.timer = setInterval(() => { if (!document.hidden) loadDetails(); }, DETAILS_MS);
}
function closePanel() {
  clearInterval(pn.timer);
  pn.id = null;
  $("#scrim").hidden = true;
  $("#panel").hidden = true;
  document.body.classList.remove("noscroll");
}
async function loadDetails() {
  const id = pn.id;
  try {
    const d = await api(`api/job/${encodeURIComponent(id)}`);
    if (pn.id !== id) return;
    pn.d = d; pn.err = null;
  } catch (e) {
    if (pn.id !== id) return;
    pn.err = e;
  }
  renderPanel();
}
async function loadLive() {
  const id = pn.id;
  pn.liveBusy = true; renderPanel();
  try {
    const live = await api(`api/job/${encodeURIComponent(id)}/live`);
    if (pn.id === id) pn.live = live;
  } catch (e) {
    if (pn.id === id) pn.live = { error: e.message, nodes: [] };
  }
  if (pn.id !== id) return;
  pn.liveBusy = false; renderPanel();
}

// ------------------------------------------------------------------ render
function renderPanel() {
  if (!pn.id) return;
  const el = $("#panel");
  const scroll = el.scrollTop;
  const logEl = el.querySelector(".log");
  const logAtEnd = !logEl || logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
  const logScroll = logEl ? logEl.scrollTop : 0;

  const snapJob = st.jobs.get(pn.id);
  const d = pn.d;
  const name = d ? d.name : snapJob ? snapJob.name : pn.id;
  let h = `<div class="ph">
    <div class="ph-t"><div class="pt">${esc(name)}</div>
      <div class="ps">${esc(pn.id)}${d ? ` · ${esc(d.state.toLowerCase())} · ${esc(d.partition)}${d.nodes.length ? " · " + esc(d.nodes.join(", ")) : ""}` : ""}</div></div>
    <button class="icon-btn" id="pclose" aria-label="Close" title="Close (Esc)">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></div>`;

  if (pn.err) {
    h += pn.err.status === 401
      ? `<div class="pnote"><b>Job details are locked.</b> They show your logs and scripts, so they need the secret link that
         <code>gpuglance serve</code> prints in its terminal (it ends in <code>?token=…</code>). Open that link once; this browser remembers it.</div>`
      : `<div class="pnote">${esc(pn.err.message)}</div>`;
  }
  if (!d) { el.innerHTML = h + (pn.err ? "" : `<div class="pnote muted">Loading…</div>`); return; }

  const T = now();
  // --- time
  if (d.state === "RUNNING") {
    const frac = d.limit ? Math.min(1, (T - d.start) / d.limit) : 0;
    const b = bucketOf({ end: d.end });
    h += `<div class="ptime">${ring(1 - frac, b.cls)}
      <div><div class="big2">${dur(d.end - T)} <small>left</small></div>
      <div class="ps">ends ${clock(d.end)} · running ${dur(T - d.start)} of ${dur(d.limit)}</div></div></div>`;
  } else if (d.state === "PENDING") {
    h += `<div class="ptime">${ring(0, "", true)}<div>
      <div class="big2">${d.est_start ? "~" + dur(d.est_start - T) : "queued"} <small>${d.est_start ? "until est. start · " + clock(d.est_start) : ""}</small></div>
      <div class="ps">waiting ${dur(T - d.submit)} · reason: ${esc(d.reason)}</div></div></div>`;
  } else {
    h += `<div class="pnote">${esc(d.state.toLowerCase())}${d.reason && d.reason !== "None" ? " · " + esc(d.reason) : ""}</div>`;
  }

  // --- resources
  const label = d.gpu_model ? modelLabel(d.gpu_model) : "GPU";
  if (d.state === "RUNNING") {
    const u = d.usage;
    const elapsed = Math.max(1, T - d.start);
    const busy = u ? u.cpu_seconds / elapsed : null;
    h += `<div class="psec"><div class="psh"><h3>Resources</h3></div><div class="ptiles">
      <div class="ptile"><div class="tk">CPU cores</div>
        <div class="tv">${busy != null ? busy.toFixed(1) : "–"} <small>of ${d.cpus} busy</small></div>
        ${meter(busy != null ? busy / d.cpus : 0, "--cpu")}<div class="tm">average since start</div></div>
      <div class="ptile"><div class="tk">Memory (RAM)</div>
        <div class="tv">${u ? fmtBytes(u.mem_peak) : "–"} <small>peak</small></div>
        ${meter(u && d.mem_alloc ? u.mem_peak / d.mem_alloc : 0, "--ink-2")}<div class="tm">of ${fmtBytes(d.mem_alloc)}${u ? " · avg " + fmtBytes(u.mem_avg) : ""}</div></div>
      <div class="ptile"><div class="tk">Disk I/O</div>
        <div class="tv">${u ? fmtBytes(u.disk_read) : "–"} <small>read</small></div>
        <div class="tm" style="margin-top:9px">${u ? fmtBytes(u.disk_written) + " written" : ""}</div></div>
    </div></div>`;

    // --- GPUs + processes (live probe)
    const L = pn.live;
    const ago = L && L.at ? dur(T - L.at) + " ago" : "";
    h += `<div class="psec"><div class="psh"><h3>${d.gpus ? `GPUs · ${d.gpus}× ${esc(label)}` : "Live"}</h3>
      <button class="lnk" id="prefresh" ${pn.liveBusy ? "disabled" : ""} title="Runs nvidia-smi and ps inside the job (srun --overlap). Each check adds a short step to the job's sacct history.">
      ${pn.liveBusy ? "checking…" : "↻ refresh"}</button></div>`;
    if (L && L.error && !L.nodes.length) h += `<div class="pnote">Couldn't look inside the job: ${esc(L.error)}</div>`;
    else if (!L) h += `<div class="pnote muted">Checking GPUs and processes…</div>`;
    else {
      const gpus = L.nodes.flatMap((n) => n.gpus.map((g) => ({ host: n.host, ...g })));
      h += gpus.length ? `<div class="gpus">` + gpus.map((g) => `
        <div class="gpu">
          <div class="gl"><b>${esc(g.host)} · GPU ${g.index}</b><span class="tm">${g.power != null ? Math.round(g.power) + " W" : ""}${g.temp != null ? " · " + Math.round(g.temp) + "°C" : ""}</span></div>
          <div class="gm"><span class="tm">util</span>${meter((g.util || 0) / 100, "--b2")}<span class="gv">${g.util != null ? Math.round(g.util) + "%" : "–"}</span></div>
          <div class="gm"><span class="tm">mem</span>${meter(g.mem_total ? g.mem_used / g.mem_total : 0, "--ink-2")}<span class="gv">${g.mem_used != null ? (g.mem_used / 1024).toFixed(1) + " / " + Math.round(g.mem_total / 1024) + " GB" : "–"}</span></div>
        </div>`).join("") + `</div>` : d.gpus ? `<div class="pnote muted">No GPU readings returned.</div>` : "";
      const procs = L.nodes.flatMap((n) => n.procs.map((p) => ({ host: n.host, ...p })));
      h += `<div class="psh" style="margin-top:14px"><h3>What's running</h3><span class="tm">${ago}</span></div>`;
      h += procs.length ? `<div class="procs">` + procs.map((p) => `
        <div class="proc"><code title="${esc(p.args)}">${p.count > 1 ? `<span class="cnt">${p.count}×</span>` : ""}${esc(p.args)}</code>
          <span class="tm">${Math.round(p.pcpu)}% CPU · ${fmtBytes(p.rss)}${L.nodes.length > 1 ? " · " + esc(p.host) : ""}</span></div>`).join("") + `</div>`
        : `<div class="pnote muted">No processes found inside the job${d.batch ? "" : " (interactive jobs only show processes started with srun)"}.</div>`;
    }
    h += `</div>`;
  } else {
    h += `<div class="psec"><div class="psh"><h3>Requested</h3></div><div class="ptiles">
      <div class="ptile"><div class="tk">GPUs</div><div class="tv">${d.gpus || 0} <small>${esc(label)}</small></div></div>
      <div class="ptile"><div class="tk">CPU cores</div><div class="tv">${d.cpus}</div></div>
      <div class="ptile"><div class="tk">Memory</div><div class="tv">${fmtBytes(d.mem_alloc)}</div></div></div></div>`;
  }

  // --- logs
  if (d.logs && d.logs.length) {
    const i = Math.min(pn.logTab, d.logs.length - 1);
    const lg = d.logs[i];
    h += `<div class="psec"><div class="psh"><h3>Log</h3>
      ${d.logs.length > 1 ? `<div class="seg small" id="logtabs">${d.logs.map((l, k) =>
        `<button data-k="${k}" class="${k === i ? "on" : ""}">${l.kind}${l.size === 0 ? " (empty)" : ""}</button>`).join("")}</div>` : ""}</div>
      <div class="lpath"><code>${esc(lg.path)}</code>${copyBtn(lg.path)}</div>
      <div class="tm">${lg.error ? esc(lg.error) : lg.mtime ? `last written ${dur(T - lg.mtime)} ago · ${fmtBytes(lg.size)}${lg.truncated ? ` · showing the last ${lg.lines.length} lines` : ""}` : ""}</div>
      ${lg.lines && lg.lines.length ? `<pre class="log">${esc(lg.lines.join("\n"))}</pre>` : ""}</div>`;
  }

  // --- submission
  h += `<div class="psec"><div class="psh"><h3>Submission</h3></div>
    ${d.submit_line ? `<div class="kv"><span class="tm">command</span><div class="lpath"><code>${esc(d.submit_line)}</code>${copyBtn(d.submit_line)}</div></div>` : ""}
    <div class="kv"><span class="tm">directory</span><div class="lpath"><code>${esc(d.workdir)}</code>${copyBtn(d.workdir)}</div></div>
    ${d.batch && d.command ? `<div class="kv"><span class="tm">script</span><div class="lpath"><code>${esc(d.command)}</code>${copyBtn(d.command)}</div></div>` : ""}
    ${d.script ? `<details id="pscript"${pn.scriptOpen ? " open" : ""}><summary>Batch script as submitted</summary><pre class="log script">${esc(d.script)}</pre></details>` : ""}
  </div>`;

  // --- metadata
  const meta = [
    ["Job ID", esc(d.id)], ["Account", esc(d.account)], ["QOS", esc(d.qos)], ["Partition", esc(d.partition)],
    ["Nodes", Object.entries(d.gpu_idx || {}).map(([n, idx]) => `${esc(n)}${idx.length ? ` [GPU ${idx.join(",")}]` : ""}`).join(", ") || esc(d.nodes.join(", ")) || "–"],
    ["Submitted", d.submit ? `${clock(d.submit)} · ${dur(T - d.submit)} ago` : "–"],
    ["Started", d.start && d.state === "RUNNING" ? clock(d.start) : "–"],
    ["Time limit", dur(d.limit)],
    ["Priority", d.priority != null ? String(d.priority) : "–"],
  ];
  if (d.array) meta.push(["Array tasks", esc(d.array)]);
  if (d.dependency) meta.push(["Dependency", esc(d.dependency)]);
  h += `<div class="psec"><div class="psh"><h3>Details</h3></div><dl class="pmeta">${meta.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    <div class="tm" style="margin-top:10px">Cancel: <code>scancel ${esc(String(d.job_id))}</code> ${copyBtn("scancel " + d.job_id)}</div></div>`;

  el.innerHTML = h;
  el.scrollTop = scroll;
  const log = el.querySelector(".log:not(.script)");
  if (log) log.scrollTop = logAtEnd ? log.scrollHeight : logScroll;
}

// ------------------------------------------------------------------ events
document.addEventListener("click", (e) => {
  if (e.target.closest("#panel")) return;
  const el = e.target.closest("[data-job]");
  const job = el && st.jobs.get(el.dataset.job);
  if (job && job.mine) { e.preventDefault(); openPanel(job.id); }
});
$("#scrim").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && pn.id) closePanel(); });
$("#panel").addEventListener("click", (e) => {
  if (e.target.closest("#pclose")) return closePanel();
  if (e.target.closest("#prefresh")) return loadLive();
  const tab = e.target.closest("#logtabs button");
  if (tab) { pn.logTab = +tab.dataset.k; const l = $("#panel .log"); if (l) l.scrollTop = 1e9; return renderPanel(); }
  const c = e.target.closest(".copy");
  if (c) {
    navigator.clipboard?.writeText(c.dataset.copy).then(() => { c.textContent = "copied"; setTimeout(() => (c.textContent = "copy"), 1200); });
  }
});
$("#panel").addEventListener("toggle", (e) => { if (e.target.id === "pscript") pn.scriptOpen = e.target.open; }, true);
