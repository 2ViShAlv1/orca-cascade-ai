/* ORCA transport layer.
 *
 * "server" mode : talks to `python run.py` (JSON API + Server-Sent Events).
 * "browser" mode: no server found (e.g. GitHub Pages) - loads Pyodide (CPython
 *                 compiled to WebAssembly) and runs the very same `orca` Python
 *                 package inside the page, routed through orca/api.py.
 */
"use strict";

const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const ORCA_MODULES = ["__init__", "topology", "simulator", "logparse", "observer", "predictor", "rca",
  "recovery", "scenarios", "report", "engine", "api"];

const PY_GLUE = `
import json, sys
sys.path.insert(0, "/orca_app")
from orca.api import handle_get, handle_post
from orca.engine import Engine

ENGINE = Engine(seed=7)

def api_get(path, query):
    status, obj = handle_get(ENGINE, path, query)
    return json.dumps({"status": status, "body": obj}, separators=(",", ":"))

def api_post(path, body_json):
    status, obj = handle_post(ENGINE, path, json.loads(body_json or "{}"))
    return json.dumps({"status": status, "body": obj}, separators=(",", ":"))

def api_tick():
    if not ENGINE.paused:
        ENGINE.step()
    return json.dumps(ENGINE.snapshot, separators=(",", ":"))

def api_snapshot():
    return json.dumps(ENGINE.snapshot, separators=(",", ":"))
`;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.appendChild(s);
  });
}

const Backend = {
  mode: null,
  listeners: [],

  async init(progress) {
    const forced = new URLSearchParams(location.search).get("engine");
    if (forced !== "browser") {
      try {
        const r = await fetch("/api/meta", { cache: "no-store" });
        if (r.ok && (r.headers.get("content-type") || "").includes("json")) {
          this.mode = "server";
          return;
        }
      } catch (e) { /* no server: fall through to the in-browser engine */ }
    }
    this.mode = "browser";
    progress("Downloading the Python runtime (Pyodide, about 10 MB, first visit only)…");
    await loadScript(PYODIDE_URL + "pyodide.js");
    const py = await loadPyodide({ indexURL: PYODIDE_URL });
    progress("Loading the ORCA engine and its trained model…");
    py.FS.mkdirTree("/orca_app/orca");
    py.FS.mkdirTree("/orca_app/models");
    py.FS.mkdirTree("/orca_app/reports");
    const fetchText = async (path) => {
      const r = await fetch(path, { cache: "no-cache" });
      if (!r.ok) throw new Error(`missing ${path}`);
      return r.text();
    };
    const sources = await Promise.all(ORCA_MODULES.map((m) => fetchText(`orca/${m}.py`)));
    ORCA_MODULES.forEach((m, i) => py.FS.writeFile(`/orca_app/orca/${m}.py`, sources[i]));
    py.FS.writeFile("/orca_app/models/predictor.json", await fetchText("models/predictor.json"));
    try { py.FS.writeFile("/orca_app/reports/evaluation.json", await fetchText("reports/evaluation.json")); }
    catch (e) { /* evaluation report is optional */ }
    progress("Warming up 17 simulated services…");
    await new Promise((r) => setTimeout(r, 30)); // let the message paint before the CPU-bound start
    py.runPython(PY_GLUE);
    this.py = {
      get: py.globals.get("api_get"),
      post: py.globals.get("api_post"),
      tick: py.globals.get("api_tick"),
      snapshot: py.globals.get("api_snapshot"),
    };
  },

  // -> {status, body}
  async get(pathWithQuery) {
    if (this.mode === "server") {
      const r = await fetch(pathWithQuery, { cache: "no-store" });
      return { status: r.status, body: await r.json() };
    }
    const [path, query = ""] = pathWithQuery.split("?");
    return JSON.parse(this.py.get(path, query));
  },

  async post(path, body) {
    if (this.mode === "server") {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      return { status: r.status, body: await r.json() };
    }
    const res = JSON.parse(this.py.post(path, JSON.stringify(body || {})));
    this._emit(JSON.parse(this.py.snapshot())); // the server pushes a snapshot after every command too
    return res;
  },

  subscribe(onSnap, onError) {
    this.listeners.push(onSnap);
    if (this.mode === "server") {
      const es = new EventSource("/api/stream");
      es.onmessage = (e) => onSnap(JSON.parse(e.data));
      es.onerror = () => onError && onError();
      return;
    }
    if (this._loop) return;
    this._loop = true;
    const step = () => {
      const t0 = performance.now();
      let speed = 1;
      try {
        const snap = JSON.parse(this.py.tick());
        speed = snap.speed || 1;
        this._emit(snap);
      } catch (e) {
        console.error(e);
        if (onError) onError();
      }
      const delay = 1000 / speed - (performance.now() - t0);
      setTimeout(step, Math.max(15, delay));
    };
    setTimeout(step, 1000);
  },

  _emit(snap) { for (const fn of this.listeners) fn(snap); },
};
window.Backend = Backend;
