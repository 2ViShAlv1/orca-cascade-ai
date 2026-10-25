/* ORCA control room - live dashboard (no dependencies). */
"use strict";

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const S = { meta: null, snap: null, hist: {}, logs: [], selected: null, built: false };
const HIST_LEN = 120;
const ALERT = () => (S.meta && S.meta.alert) || 0.6;

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (v, d = 1) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}
function fmtEta(ticks) {
  if (ticks == null) return "–";
  if (ticks <= 0) return "now";
  const secs = Math.round(ticks * (S.meta ? S.meta.tick_seconds : 5));
  const human = secs >= 60 ? `${Math.floor(secs / 60)}m ${String(secs % 60).padStart(2, "0")}s` : `${secs}s`;
  return `${Math.round(ticks)} ticks (~${human})`;
}
function fmtClock(tick) {
  const secs = tick * (S.meta ? S.meta.tick_seconds : 5);
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = Math.floor(secs % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
async function post(path, body) {
  const { status, body: j } = await Backend.post(path, body);
  if (status >= 400 || j.ok === false) { toast(j.error || "Request failed"); throw new Error(j.error); }
  return j;
}
let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// status: color token + icon + label (never color alone)
const STATUS = {
  healthy: { c: "var(--good)", label: "Healthy", icon: "check" },
  degraded: { c: "var(--warning)", label: "Degraded", icon: "warn" },
  critical: { c: "var(--serious)", label: "Critical", icon: "warn" },
  failed: { c: "var(--critical)", label: "Failed", icon: "x" },
  down: { c: "var(--critical)", label: "Down", icon: "x" },
};
function iconSvg(kind, color, size = 12) {
  if (kind === "check") return `<svg width="${size}" height="${size}" viewBox="0 0 12 12"><circle cx="6" cy="6" r="6" fill="${color}"/><path d="M3.2 6.2l1.9 1.9 3.7-4" stroke="#fff" stroke-width="1.6" fill="none"/></svg>`;
  if (kind === "warn") return `<svg width="${size}" height="${size}" viewBox="0 0 12 12"><path d="M6 .6l5.6 10.4H.4z" fill="${color}"/><rect x="5.3" y="4" width="1.4" height="3.6" fill="#0b0b0b"/><rect x="5.3" y="8.4" width="1.4" height="1.4" fill="#0b0b0b"/></svg>`;
  return `<svg width="${size}" height="${size}" viewBox="0 0 12 12"><circle cx="6" cy="6" r="6" fill="${color}"/><path d="M3.8 3.8l4.4 4.4M8.2 3.8l-4.4 4.4" stroke="#fff" stroke-width="1.6"/></svg>`;
}
function statusBadge(status) {
  const s = STATUS[status] || STATUS.healthy;
  return `<span class="status">${iconSvg(s.icon, s.c)}${s.label}</span>`;
}
function riskColor(r) {
  if (r >= 0.75) return "var(--critical)";
  if (r >= 0.5) return "var(--serious)";
  if (r >= 0.25) return "var(--warning)";
  return "var(--good)";
}

// ---------------------------------------------------------------- theme
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("btnTheme").textContent = `Theme: ${t}`;
}
function initTheme() {
  let t = new URLSearchParams(location.search).get("theme");
  if (!t) { try { t = localStorage.getItem("orca-theme"); } catch (e) { t = null; } }
  applyTheme(THEMES.includes(t) ? t : "auto");
  $("btnTheme").onclick = () => {
    const cur = document.documentElement.dataset.theme || "auto";
    const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
    applyTheme(next);
    try { localStorage.setItem("orca-theme", next); } catch (e) { /* storage unavailable */ }
  };
}

// ---------------------------------------------------------------- bootstrap
async function init() {
  initTheme();
  await Backend.init((msg) => { $("briefText").textContent = msg; $("sysStatus").textContent = "starting…"; });
  if (Backend.mode === "browser") {
    $("modeTag").textContent = "running in your browser";
    $("modeTag").title = "No server needed: the Python engine runs in this page via WebAssembly (Pyodide)";
    $("modeTag").hidden = false;
  }
  S.meta = (await Backend.get("/api/meta")).body;
  const full = (await Backend.get("/api/state")).body;
  for (const svc of S.meta.topology.services) {
    const mh = full.metric_history[svc.name];
    S.hist[svc.name] = {
      err: mh.err.slice(-HIST_LEN), lat: mh.lat.slice(-HIST_LEN), cpu: mh.cpu.slice(-HIST_LEN),
      mem: mh.mem.slice(-HIST_LEN), sat: mh.sat.slice(-HIST_LEN),
      risk: full.risk_history[svc.name].slice(-HIST_LEN),
    };
  }
  S.logs = full.log_tail.slice(-300);
  S.newLogs = true;
  buildControls();
  buildGraph();
  S.lastSeq = full.seq;
  render(full);
  const params = new URLSearchParams(location.search);
  if (params.get("open")) openDrawer(params.get("open"));
  const show = params.get("show") || ""; // deep links: ?show=model | ?show=incident-<id>
  if (show === "model") showModelCard();
  else if (show.startsWith("incident-")) showPostmortem(Number(show.slice(9)));
  if (!params.has("once")) connect(); // ?once = static capture
}

function connect() {
  Backend.subscribe((snap) => {
    if (S.snap && snap.tick < S.snap.tick) { // engine was reset
      for (const n in S.hist) for (const k in S.hist[n]) S.hist[n][k] = [];
      S.logs = [];
    }
    if (!S.snap || snap.tick !== S.snap.tick) ingest(snap);
    render(snap);
  }, () => { $("sysStatus").innerHTML = "reconnecting…"; });
}

function ingest(snap) {
  for (const [n, s] of Object.entries(snap.services)) {
    const h = S.hist[n];
    if (!h) continue;
    for (const k of ["err", "lat", "cpu", "mem", "sat"]) { h[k].push(s[k]); if (h[k].length > HIST_LEN) h[k].shift(); }
    h.risk.push(s.cascade); if (h.risk.length > HIST_LEN) h.risk.shift();
  }
  for (const l of snap.logs) S.logs.push(l);
  if (S.logs.length > 300) S.logs.splice(0, S.logs.length - 300);
  S.newLogs = true;
}

// ---------------------------------------------------------------- controls
function buildControls() {
  const m = S.meta;
  $("selPreset").innerHTML = m.presets.map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("");
  $("selFault").innerHTML = m.faults.map((f) => `<option value="${f.id}" title="${esc(f.description)}">${esc(f.label)}</option>`).join("");
  const fillTargets = () => {
    const f = m.faults.find((x) => x.id === $("selFault").value);
    $("selTarget").innerHTML = f.targets.map((t) => `<option>${t}</option>`).join("");
    $("selFault").title = f.description;
  };
  $("selFault").onchange = fillTargets;
  fillTargets();
  $("rngSev").oninput = () => ($("outSev").textContent = Number($("rngSev").value).toFixed(1));
  $("btnPreset").onclick = () => post("/api/scenario", { id: $("selPreset").value }).then(() => toast("Scenario started"));
  $("btnInject").onclick = () =>
    post("/api/inject", { kind: $("selFault").value, target: $("selTarget").value, severity: Number($("rngSev").value) })
      .then(() => toast(`Injected ${$("selFault").selectedOptions[0].text} on ${$("selTarget").value}`));
  $("btnPause").onclick = () => post("/api/control", { paused: !S.snap.paused });
  $("btnStep").onclick = () => post("/api/control", { step: true });
  $("selSpeed").onchange = () => post("/api/control", { speed: Number($("selSpeed").value) });
  $("chkAuto").onchange = () => post("/api/control", { autopilot: $("chkAuto").checked });
  $("btnReset").onclick = () => post("/api/control", { reset: true }).then(() => toast("Environment reset"));
  $("logSvc").innerHTML += m.topology.services.map((s) => `<option>${s.name}</option>`).join("");
  $("logLevel").onchange = $("logSvc").onchange = () => { S.newLogs = true; renderLogs(true); };
  $("dClose").onclick = closeDrawer;
  $("mClose").onclick = closeModal;
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("btnModel").onclick = showModelCard;
  $("incidents").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-inc]");
    if (b) showPostmortem(Number(b.dataset.inc));
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });
  $("planTable").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-sid]");
    if (b) post("/api/strategy", { id: Number(b.dataset.sid) }).then(() => toast("Strategy applied"));
  });
  $("forecast").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-svc]");
    if (tr) openDrawer(tr.dataset.svc);
  });
  $("dBody").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (b) post("/api/action", { kind: b.dataset.act, target: S.selected }).then(() => toast(`${b.textContent} → ${S.selected}`));
  });
  const meta = m.model || {};
  if (meta.val_auc) $("modelInfo").textContent = `${meta.model || "model"} · validation AUC ${meta.val_auc.toFixed(3)} · horizon ${meta.horizon} ticks`;
  $("graphLegend").innerHTML = Object.entries(STATUS).filter(([k]) => k !== "down")
    .map(([, s]) => `<span>${iconSvg(s.icon, s.c, 11)} ${s.label}</span>`).join("");
}

// ---------------------------------------------------------------- graph
const NODE_W = 138, NODE_H = 50, GW = 1000, GH = 470;
function nodePos(svc) { return { x: svc.x * GW, y: 18 + svc.y * (GH - 36) }; }

function buildGraph() {
  const g = $("graph");
  g.innerHTML = "";
  const defs = svgEl("defs", {}, g);
  for (const [id, color] of [["arr", "var(--axis)"], ["arrWarn", "var(--serious)"], ["arrBad", "var(--critical)"]]) {
    const mk = svgEl("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
    svgEl("path", { d: "M0 0L10 5L0 10z", fill: color }, mk);
  }
  const byName = {};
  for (const s of S.meta.topology.services) byName[s.name] = s;
  const edgeLayer = svgEl("g", {}, g);
  S.edgeEls = {};
  for (const d of S.meta.topology.dependencies) {
    const a = nodePos(byName[d.caller]), b = nodePos(byName[d.callee]);
    const y1 = a.y + NODE_H / 2, y2 = b.y - NODE_H / 2 - 2;
    const my = (y1 + y2) / 2;
    const p = svgEl("path", { d: `M${a.x} ${y1} C${a.x} ${my} ${b.x} ${my} ${b.x} ${y2}`, class: "edge" + (d.hard ? "" : " soft"), "marker-end": "url(#arr)" }, edgeLayer);
    const t = svgEl("title", {}, p);
    t.textContent = `${d.caller} → ${d.callee} (${d.hard ? "hard" : "soft"} dependency, ${d.calls} calls/request)`;
    S.edgeEls[d.caller + ">" + d.callee] = { el: p, d };
  }
  const nodeLayer = svgEl("g", {}, g);
  S.nodeEls = {};
  for (const s of S.meta.topology.services) {
    const { x, y } = nodePos(s);
    const grp = svgEl("g", { class: "node", transform: `translate(${x - NODE_W / 2} ${y - NODE_H / 2})`, tabindex: 0 }, nodeLayer);
    svgEl("rect", { class: "box", width: NODE_W, height: NODE_H, rx: 8 }, grp);
    const icon = svgEl("g", { transform: "translate(8 8)" }, grp);
    const name = svgEl("text", { x: 25, y: 18 }, grp);
    name.textContent = s.name.length > 17 ? s.name.replace("-service", "") : s.name;
    const meta = svgEl("text", { x: 8, y: 33, class: "meta" }, grp);
    svgEl("line", { class: "risk-track", x1: 8, x2: NODE_W - 8, y1: 42, y2: 42 }, grp);
    const arc = svgEl("line", { class: "risk-arc", x1: 8, x2: 8, y1: 42, y2: 42 }, grp);
    const tagG = svgEl("g", { transform: `translate(${NODE_W / 2 - 40} -15)`, visibility: "hidden" }, grp);
    svgEl("rect", { class: "root-tag", width: 80, height: 15, rx: 4 }, tagG);
    const tagT = svgEl("text", { class: "root-tag-text", x: 40, y: 11, "text-anchor": "middle" }, tagG);
    tagT.textContent = "ROOT CAUSE";
    const eta = svgEl("text", { class: "eta", x: NODE_W / 2, y: NODE_H + 13, "text-anchor": "middle" }, grp);
    const title = svgEl("title", {}, grp);
    grp.addEventListener("click", () => openDrawer(s.name));
    grp.addEventListener("keydown", (e) => { if (e.key === "Enter") openDrawer(s.name); });
    S.nodeEls[s.name] = { grp, icon, meta, arc, tagG, eta, title };
  }
}

function renderGraph(snap) {
  const rca = snap.rca;
  const prop = new Set((rca ? rca.propagation : []).map(([a, b]) => a + ">" + b));
  for (const [key, { el, d }] of Object.entries(S.edgeEls)) {
    const callee = snap.services[d.callee];
    let cls = "edge" + (d.hard ? "" : " soft"), marker = "url(#arr)";
    if (callee.breaker) cls += " breaker";
    else if (callee.status === "failed" || callee.status === "down") { cls += " bad"; marker = "url(#arrBad)"; }
    else if (callee.status === "critical") { cls += " warn"; marker = "url(#arrWarn)"; }
    if (prop.has(key)) cls += " flow" + (cls.includes("bad") || cls.includes("warn") ? "" : " warn");
    el.setAttribute("class", cls);
    el.setAttribute("marker-end", marker);
  }
  for (const [n, els] of Object.entries(S.nodeEls)) {
    const s = snap.services[n];
    const st = STATUS[s.status];
    els.icon.innerHTML = iconSvg(st.icon, st.c, 13);
    let meta = s.status === "down" ? "container down" : `${s.rps.toFixed(0)} rps · err ${pct(s.err)}`;
    if (s.pending) meta += ` · +${s.pending} pods`;
    if (s.breaker) meta += " · breaker";
    els.meta.textContent = meta;
    const r = s.cascade;
    els.arc.setAttribute("x2", 8 + (NODE_W - 16) * Math.max(r, 0.02));
    els.arc.setAttribute("stroke", riskColor(r));
    els.tagG.setAttribute("visibility", rca && rca.root === n ? "visible" : "hidden");
    const failing = s.status === "failed" || s.status === "down";
    els.eta.textContent = !failing && r >= ALERT() && s.eta != null ? `fails in ~${Math.round(s.eta)} ticks` : "";
    els.grp.classList.toggle("sel", S.selected === n);
    els.title.textContent = `${n}: ${st.label}, cascade risk ${pct(r, 0)}`;
  }
}

// ---------------------------------------------------------------- panels
function renderHeader(snap) {
  const map = { nominal: ["Nominal", "var(--good)"], degraded: ["Degraded", "var(--warning)"], critical: ["Critical", "var(--serious)"], outage: ["Outage", "var(--critical)"] };
  const [label, color] = map[snap.status];
  const icon = { nominal: "check", degraded: "warn", critical: "warn", outage: "x" }[snap.status];
  $("sysStatus").innerHTML = `${iconSvg(icon, color, 12)} ${label}${snap.incident ? ` · Incident #${snap.incident.id}` : ""}`;
  $("kAvail").textContent = pct(snap.availability, 2);
  $("kIngress").textContent = `${snap.ingress.toFixed(0)} req/s${snap.rate_limit ? " (limited)" : ""}`;
  const sm = snap.summary;
  $("kCrash").textContent = `${pct(sm.system_crash_risk, 0)}${sm.system_crash_eta != null && sm.system_crash_risk >= ALERT() && sm.system_crash_risk < 1 ? " · ~" + Math.round(sm.system_crash_eta) + "t" : ""}`;
  $("kFail").textContent = `${sm.failed.length} / ${sm.at_risk.length}`;
  $("kClock").textContent = `${fmtClock(snap.tick)} · t${snap.tick}`;
  $("btnPause").textContent = snap.paused ? "Resume" : "Pause";
  $("chkAuto").checked = snap.autopilot;
  $("selSpeed").value = String(snap.speed);
  $("chaosActive").innerHTML = snap.chaos.length
    ? "Active chaos (hidden from ORCA): " + snap.chaos.map((c) => `<span class="tag">${esc(S.meta.faults.find((f) => f.id === c.kind).label)} → ${c.target} since t${c.since}</span>`).join("")
    : "No chaos active - ORCA only sees metrics, logs and change events.";
}

function renderForecast(snap) {
  const rows = Object.entries(snap.services).sort((a, b) => b[1].cascade - a[1].cascade || a[0].localeCompare(b[0])).slice(0, 10);
  let html = `<thead><tr><th>Service</th><th>Status</th><th>Cascade risk (15 ticks)</th><th>Predicted failure</th><th>Why</th></tr></thead><tbody>`;
  for (const [n, s] of rows) {
    const failing = s.status === "failed" || s.status === "down";
    let why = "";
    if (failing) why = s.status === "down" ? "container down" : `error rate ${pct(s.err, 0)}`;
    else if (s.via) why = `cascade via <b>${esc(s.via)}</b>`;
    else if (s.drivers.length) why = esc(s.drivers.slice(0, 2).map((d) => d.label).join(", "));
    else why = "nominal";
    html += `<tr class="clickable" data-svc="${n}"><td>${n}</td><td>${statusBadge(s.status)}</td>
      <td><div style="display:flex;gap:8px;align-items:center"><div class="bar" style="flex:1"><i style="width:${(s.cascade * 100).toFixed(0)}%;background:${riskColor(s.cascade)}"></i></div><span class="num" style="width:36px">${pct(s.cascade, 0)}</span></div></td>
      <td class="num">${failing ? "failing now" : s.cascade >= ALERT() ? fmtEta(s.eta) : "–"}</td>
      <td class="driver">${why}</td></tr>`;
  }
  $("forecast").innerHTML = html + "</tbody>";
}

function renderRca(snap) {
  const r = snap.rca;
  if (!r) {
    $("rca").innerHTML = `<div class="empty"><span class="big">No active incident</span>All services within learned baselines. Inject chaos to watch ORCA work.</div>`;
    return;
  }
  const conf = r.confidence;
  let html = `<div class="rca-head"><div><div class="rca-root">${esc(r.root)}</div><div class="rca-kind">${esc(r.label)}</div></div>
    <div class="conf"><label>confidence</label><b>${pct(conf, 0)}</b></div></div>`;
  html += `<div class="sub">Evidence</div><ul class="evidence">${r.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`;
  html += `<div class="sub" style="margin-bottom:4px">Candidate ranking (random walk over blame + correlation graph)</div>`;
  for (const c of r.ranking) {
    html += `<div class="rank-row"><span>${esc(c.service)}</span><div class="bar"><i style="width:${(c.score * 100).toFixed(0)}%;background:var(--series-1)"></i></div><span class="num">${pct(c.score, 0)}</span></div>`;
  }
  if (r.affected.length) {
    html += `<div class="chain"><b>Blast radius:</b> ${r.affected.map(esc).join(", ")}</div>`;
  }
  $("rca").innerHTML = html;
}

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
function renderPlan(snap) {
  const plan = snap.plan;
  if (!plan) {
    $("planHeadline").textContent = "";
    $("planSub").textContent = "";
    $("planChart").innerHTML = "";
    $("planLegend").innerHTML = "";
    $("planTable").innerHTML = `<tbody><tr><td class="empty">Strategies appear when an incident is detected. ORCA forks a digital twin under its diagnosis and simulates each option forward.</td></tr></tbody>`;
    return;
  }
  $("planHeadline").textContent = plan.headline;
  $("planSub").textContent = `what-if on digital twin · hypothesis: ${plan.hypothesis.kind} @ ${plan.hypothesis.root} · computed t${plan.tick} · horizon ${plan.horizon} ticks`;
  const strategies = plan.strategies;
  const baseline = strategies.find((s) => s.id === plan.baseline);
  const shown = strategies.filter((s) => s.actions.length).slice(0, 4);
  const lines = shown.map((s, i) => ({ label: s.label, values: s.series, color: SERIES[i], s }));
  lines.push({ label: "No action", values: baseline.series, color: "var(--muted)", dash: true, s: baseline });
  drawLines($("planChart"), $("planTip"), lines, { xLabel: (i) => `+${i + 1}t`, xTicks: [0, 14, 29, 44, 59] });
  $("planLegend").innerHTML = lines.map((l) => `<span><i class="sw${l.dash ? " dash" : ""}" style="background:${l.color}"></i>${esc(l.label)}</span>`).join("");
  let html = `<thead><tr><th>#</th><th>Strategy</th><th class="num">Proj. availability</th><th class="num">vs no action</th><th class="num">Recovers in</th><th class="num">Peak failed</th><th class="num">+Pods</th><th class="num">Score</th><th></th></tr></thead><tbody>`;
  for (const s of strategies) {
    const best = s.id === plan.best;
    html += `<tr class="${best ? "best" : ""}" title="${esc(s.rationale)}"><td>${s.rank}</td><td>${esc(s.label)}<div class="sub">${esc(s.rationale)}</div></td>
      <td class="num">${pct(s.availability)}</td><td class="num">${s.actions.length ? (s.delta >= 0 ? "+" : "") + (s.delta * 100).toFixed(1) + " pts" : "–"}</td>
      <td class="num">${s.recover_ticks == null ? "not within horizon" : s.recover_ticks + " ticks"}</td><td class="num">${s.peak_failed}</td>
      <td class="num">${s.extra_pods || "–"}</td><td class="num">${s.score.toFixed(1)}</td>
      <td>${s.actions.length ? `<button class="apply${best ? " best" : ""}" data-sid="${s.id}">Apply</button>` : ""}</td></tr>`;
  }
  $("planTable").innerHTML = html + "</tbody>";
}

// generic multi-line chart with crosshair tooltip (y = 0..100%)
function drawLines(svg, tip, lines, opt) {
  const W = 640, H = svg.viewBox.baseVal.height, L = 34, R = 8, T = 8, B = 18;
  const n = Math.max(...lines.map((l) => l.values.length));
  const x = (i) => L + (n <= 1 ? 0 : (i / (n - 1)) * (W - L - R));
  const ymin = opt.yMin ?? 0;
  const y = (v) => T + (1 - (v - ymin) / (1 - ymin)) * (H - T - B);
  let html = "";
  for (const g of [0, 0.25, 0.5, 0.75, 1]) {
    const v = ymin + g * (1 - ymin);
    html += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-label" x="${L - 5}" y="${y(v) + 3}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`;
  }
  for (const i of opt.xTicks || []) if (i < n) html += `<text class="axis-label" x="${x(i)}" y="${H - 4}" text-anchor="middle">${opt.xLabel(i)}</text>`;
  if (opt.markers) html += opt.markers(x, y, T, H - B);
  for (const l of lines.slice().reverse()) {
    const d = l.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(Math.max(ymin, v)).toFixed(1)}`).join("");
    html += `<path class="series" d="${d}" stroke="${l.color}" ${l.dash ? 'stroke-dasharray="5 4"' : ""}/>`;
  }
  html += `<line class="crosshair" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>`;
  html += `<rect x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent" class="hit"/>`;
  svg.innerHTML = html;
  const cross = svg.querySelector(".crosshair");
  const hit = svg.querySelector(".hit");
  hit.onmousemove = (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    let body = `<div><b>${opt.xLabel(i)}</b></div>`;
    for (const l of lines) if (l.values[i] != null) body += `<div class="row"><i class="sw" style="background:${l.color}"></i>${esc(l.label)}: <b>${pct(l.values[i])}</b></div>`;
    if (opt.extra) body += opt.extra(i);
    tip.innerHTML = body;
    tip.style.display = "block";
    const left = (x(i) / W) * rect.width;
    tip.style.left = Math.min(left + 12, rect.width - tip.offsetWidth - 4) + "px";
    tip.style.top = "6px";
  };
  hit.onmouseleave = () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; };
}

function renderAvailability(snap) {
  const hist = snap.history;
  const ticks = hist.map((h) => h[0]);
  const vals = hist.map((h) => h[1]);
  const t0 = ticks[0];
  const evs = snap.timeline.filter((e) => ["chaos", "action", "crash"].includes(e.type) && e.tick >= t0);
  const color = { chaos: "var(--series-2)", action: "var(--series-1)", crash: "var(--critical)" };
  drawLines($("availChart"), $("availTip"), [{ label: "User availability", values: vals, color: "var(--series-3)" }], {
    xLabel: (i) => `t${ticks[i]}`,
    xTicks: [0, Math.floor(vals.length / 2), vals.length - 1].filter((v, i, a) => a.indexOf(v) === i),
    markers: (x, y, top, bottom) => evs.map((e) => {
      const i = ticks.indexOf(e.tick);
      if (i < 0) return "";
      const shape = e.type === "crash" ? `<circle cx="${x(i)}" cy="${top + 3}" r="3.5" fill="${color[e.type]}"/>`
        : `<rect x="${x(i) - 3.5}" y="${top - 1}" width="7" height="7" rx="1.5" fill="${color[e.type]}"/>`;
      return `<line x1="${x(i)}" x2="${x(i)}" y1="${top}" y2="${bottom}" stroke="${color[e.type]}" stroke-width="1" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/>${shape}`;
    }).join(""),
    extra: (i) => evs.filter((e) => e.tick === ticks[i]).map((e) => `<div class="sub">${esc(e.text)}</div>`).join(""),
  });
  const incs = snap.incidents.slice().reverse().slice(0, 4);
  $("incidents").innerHTML = incs.map((inc) => {
    const lead = inc.first_prediction != null && inc.first_failure != null ? inc.first_failure - inc.first_prediction : null;
    return `<div class="inc"><b>Incident #${inc.id}</b><span>${inc.closed == null ? "OPEN" : "resolved"} · t${inc.opened}${inc.closed != null ? "–t" + inc.closed : ""}</span>
      <span>root: <b>${esc(inc.root || "analysing…")}</b>${inc.label ? " (" + esc(inc.label) + ")" : ""}</span>
      <span>min availability ${pct(inc.min_availability)}</span><span>peak failed ${inc.peak_failed}</span>
      <span>${lead != null ? `predicted <b>${lead} ticks</b> before first failure` : inc.first_prediction != null && inc.first_failure == null ? "failure predicted - none occurred yet" : ""}</span>
      <span>${inc.actions.length ? "actions: " + inc.actions.map((a) => esc(a.label) + (a.source === "autopilot" ? " (auto)" : "")).join("; ") : ""}</span>
      <button data-inc="${inc.id}" title="Auto-generated incident report">Post-mortem</button></div>`;
  }).join("");
}

function renderLogs(force) {
  if (!S.newLogs && !force) return;
  S.newLogs = false;
  const box = $("logs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  const lvl = $("logLevel").value, svc = $("logSvc").value;
  const rank = { INFO: 0, WARN: 1, ERROR: 2, FATAL: 3 };
  const min = lvl ? rank[lvl] : 0;
  const rows = S.logs.filter((l) => rank[l.level] >= min && (!svc || l.service === svc)).slice(-160);
  box.innerHTML = rows.map((l) => {
    let msg = esc(l.msg);
    const blame = l.msg.match(/(?:from|to|refused:|Call to) ([a-z]+(?:-[a-z]+)*)/);
    if (blame && l.level !== "INFO" && S.hist[blame[1]]) msg = msg.replace(blame[1], `<mark>${blame[1]}</mark>`);
    return `<div class="logline"><span class="t">t${l.tick}</span> <span class="lv lv-${l.level}">${l.level}</span> <span class="svc">[${l.service}]</span> ${msg}</div>`;
  }).join("");
  if (atBottom || force) box.scrollTop = box.scrollHeight;
}

function renderTimeline(snap) {
  $("timeline").innerHTML = snap.timeline.slice().reverse().map((e) =>
    `<li><span class="tt">t${e.tick}</span><span class="ty ty-${e.type}">${e.type}</span><span>${esc(e.text)}</span></li>`).join("");
}

// ---------------------------------------------------------------- drawer
function openDrawer(n) {
  S.selected = n;
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  renderDrawer(S.snap);
  renderGraph(S.snap);
}
function closeDrawer() {
  S.selected = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  if (S.snap) renderGraph(S.snap);
}
function spark(label, values, fmt, max) {
  const n = values.length;
  const hi = max ?? Math.max(...values, 1e-6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${((i / Math.max(n - 1, 1)) * 100).toFixed(2)} ${(36 - (Math.min(v, hi) / hi) * 34).toFixed(2)}`).join("");
  return `<div class="spark"><label>${label}<b>${n ? fmt(values[n - 1]) : "–"}</b></label><svg viewBox="0 0 100 38" preserveAspectRatio="none"><path d="${d}"/></svg></div>`;
}
function renderDrawer(snap) {
  const n = S.selected;
  if (!n || !snap) return;
  const s = snap.services[n];
  const h = S.hist[n];
  const spec = S.meta.topology.services.find((x) => x.name === n);
  $("dTitle").innerHTML = `${esc(n)} ${statusBadge(s.status)}`;
  let html = `<div class="kv">
    <span>Kind</span><span>${spec.kind}${spec.stateful ? " (stateful)" : ""}</span>
    <span>Version</span><span>${esc(s.version)}</span>
    <span>Replicas</span><span>${s.replicas}${s.pending ? ` (+${s.pending} starting)` : ""}</span>
    <span>Crashes</span><span>${s.crashes}</span>
    <span>Cascade risk</span><span><b>${pct(s.cascade, 0)}</b> (direct ${pct(s.risk, 0)})${s.via ? " - via " + esc(s.via) : ""}</span>
    <span>Predicted failure</span><span>${s.cascade >= ALERT() ? fmtEta(s.eta) : "not expected"}</span>
    <span>Anomaly score</span><span>${s.anomaly.toFixed(1)}σ${s.onset != null ? ` (onset t${s.onset})` : ""}</span>
    ${s.disk != null ? `<span>Disk</span><span>${pct(s.disk, 0)}</span>` : ""}
    ${s.hit != null ? `<span>Cache hit ratio</span><span>${pct(s.hit, 0)}</span>` : ""}
  </div>`;
  if (s.drivers.length) {
    html += `<h3>Why ORCA thinks it may fail</h3><div>`;
    const maxC = Math.max(...s.drivers.map((d) => d.contribution));
    for (const d of s.drivers) {
      html += `<div class="rank-row" style="grid-template-columns:170px 1fr 44px"><span>${esc(d.label)}</span><div class="bar"><i style="width:${(d.contribution / maxC) * 100}%;background:var(--serious)"></i></div><span class="num">+${d.contribution.toFixed(2)}</span></div>`;
    }
    html += `<div class="sub">contribution to failure log-odds (tree path attribution)</div></div>`;
  }
  const zs = Object.entries(s.zs);
  if (zs.length) html += `<h3>Anomalous signals</h3><div class="kv">${zs.map(([k, v]) => `<span>${k}</span><span>${v.toFixed(1)}σ above baseline</span>`).join("")}</div>`;
  html += `<h3>Telemetry (last ${h.err.length} ticks)</h3><div class="spark-grid">
    ${spark("Error rate", h.err, (v) => pct(v, 1), 1)}
    ${spark("Latency", h.lat, (v) => v.toFixed(0) + " ms")}
    ${spark("CPU", h.cpu, (v) => pct(v, 0), 1)}
    ${spark("Memory", h.mem, (v) => pct(v, 0), 1)}
    ${spark("Pool saturation", h.sat, (v) => pct(v, 0))}
    ${spark("Cascade risk", h.risk, (v) => pct(v, 0), 1)}</div>`;
  const acts = S.meta.actions.filter((a) => a.targets.includes(n));
  html += `<h3>Manual remediation</h3><div class="actions">${acts.map((a) => `<button data-act="${a.id}" title="${esc(a.description)}">${esc(a.label)}</button>`).join("")}</div>`;
  $("dBody").innerHTML = html;
}

// ---------------------------------------------------------------- briefing, modal, reports
function renderBriefing(snap) {
  const b = snap.briefing;
  if (!b) return;
  const map = { nominal: ["check", "var(--good)"], degraded: ["warn", "var(--warning)"], critical: ["warn", "var(--serious)"], outage: ["x", "var(--critical)"] };
  const [icon, color] = map[b.level] || map.degraded;
  $("briefing").className = `briefing card lv-${b.level}`;
  $("briefIcon").innerHTML = iconSvg(icon, color, 18);
  $("briefText").textContent = b.text;
}

function mdToHtml(md) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/\b_(.+?)_\b/g, "<em>$1</em>").replace(/`(.+?)`/g, "<code>$1</code>");
  const out = [];
  const lines = md.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("# ")) out.push(`<h1>${inline(l.slice(2))}</h1>`);
    else if (l.startsWith("## ")) out.push(`<h2>${inline(l.slice(3))}</h2>`);
    else if (l.startsWith("- ")) {
      const items = [];
      while (i < lines.length && lines[i].startsWith("- ")) items.push(`<li>${inline(lines[i++].slice(2))}</li>`);
      i--;
      out.push(`<ul>${items.join("")}</ul>`);
    } else if (l.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      i--;
      const cells = (r) => r.trim().slice(1, -1).split("|").map((c) => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`);
    } else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  return out.join("");
}

function openModal(title, html, download) {
  $("mTitle").textContent = title;
  $("mBody").innerHTML = html;
  $("mDownload").hidden = !download;
  $("mDownload").onclick = download ? () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([download.text], { type: "text/markdown" }));
    a.download = download.name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } : null;
  $("modal").hidden = false;
  $("mClose").focus();
}
function closeModal() { $("modal").hidden = true; }

async function showPostmortem(id) {
  const { status, body: j } = await Backend.get(`/api/postmortem?id=${id}`);
  if (status >= 400) return toast(j.error || "No report");
  openModal(`Incident #${id} post-mortem`, mdToHtml(j.markdown), { text: j.markdown, name: `orca-incident-${id}-postmortem.md` });
}

async function showModelCard() {
  const m = S.meta.model || {};
  let html = `<h2>Failure predictor</h2><div class="stat-tiles">
    <div><b>${m.val_auc ? m.val_auc.toFixed(3) : "–"}</b><span>validation AUC (${esc(m.model || "")})</span></div>
    <div><b>${m.baseline_logreg_val_auc ? m.baseline_logreg_val_auc.toFixed(3) : "–"}</b><span>logistic-regression baseline AUC</span></div>
    <div><b>${m.samples ? m.samples.toLocaleString() : "–"}</b><span>training samples from ${m.episodes || "?"} simulated incidents</span></div>
    <div><b>${m.horizon || 15} ticks</b><span>prediction horizon (~${(m.horizon || 15) * S.meta.tick_seconds}s)</span></div></div>`;
  const r = await Backend.get("/api/evaluation");
  if (r.status < 400) {
    const e = r.body;
    const o = e.overall;
    const p = (v) => (v == null ? "n/a" : (v * 100).toFixed(0) + "%");
    const num = (v) => (v == null ? "n/a" : v.toFixed(1));
    html += `<h2>Held-out evaluation (${e.episodes} chaos episodes, ${esc(e.generated)})</h2><div class="stat-tiles">
      <div><b>${p(o.recall)}</b><span>failures predicted before they happened</span></div>
      <div><b>${num(o.lead)} ticks</b><span>mean warning (~${Math.round((o.lead || 0) * S.meta.tick_seconds)}s)</span></div>
      <div><b>${p(o.top1)}</b><span>root cause correct (top-1)</span></div>
      <div><b>${p(o.kind_acc)}</b><span>fault type correct</span></div>
      <div><b>${p(o.loop_none)} → ${p(o.loop_orca)}</b><span>availability, no action → ORCA autopilot</span></div>
      <div><b>${p(e.calm_flag_rate)}</b><span>false-alarm ticks on fault-free runs</span></div></div>
      <table><thead><tr><th>Fault</th><th>Predicted</th><th>Precision</th><th>Lead</th><th>RCA top-1</th><th>Type</th><th>No action</th><th>ORCA</th><th>Expert fix</th></tr></thead><tbody>
      ${e.per_kind.map((k) => `<tr><td>${esc(k.label)}</td><td>${p(k.recall)}</td><td>${p(k.precision)}</td><td>${num(k.lead)}</td><td>${p(k.top1)}</td><td>${p(k.kind_acc)}</td><td>${p(k.none)}</td><td>${p(k.orca)}</td><td>${p(k.expert)}</td></tr>`).join("")}
      </tbody></table><p class="sub">Availability columns: mean user availability over the 60 ticks after ORCA's first recommendation, measured on the real (ground-truth) system.</p>`;
  } else {
    html += `<p class="sub">Run <code>python -m orca.evaluate</code> to add held-out evaluation results here.</p>`;
  }
  openModal("Model card - how good is ORCA?", html, null);
}

// ---------------------------------------------------------------- render
function render(snap) {
  S.snap = snap;
  renderHeader(snap);
  renderBriefing(snap);
  renderGraph(snap);
  renderForecast(snap);
  renderRca(snap);
  renderPlan(snap);
  renderAvailability(snap);
  renderLogs();
  renderTimeline(snap);
  if (S.selected) renderDrawer(snap);
}

init().catch((e) => {
  $("sysStatus").textContent = "failed to load";
  $("briefText").textContent = "Could not start ORCA: " + e.message;
  console.error(e);
});
