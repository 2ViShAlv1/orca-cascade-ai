"""Live engine: drives the simulated estate and the analysis pipeline.

Every tick:  simulate -> ingest telemetry -> predict -> root-cause -> plan
-> (optionally) auto-remediate -> publish a snapshot for the dashboard.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .observer import Observer
from .predictor import ALERT, Predictor
from .rca import RootCauseAnalyzer
from . import report
from .recovery import RecoveryPlanner, should_act
from .scenarios import PRESETS
from .simulator import ACTION_CATALOG, FAULT_CATALOG, Simulator, action_targets, fault_targets
from .topology import Topology

WARMUP_TICKS = 35
PLAN_EVERY = 3


class Pipeline:
    """Simulator + analysis stack, without threading (also used by evaluation)."""

    def __init__(self, topo: Topology, seed: int, model=None, warmup: int = WARMUP_TICKS):
        self.topo = topo
        self.sim = Simulator(topo, seed=seed)
        self.obs = Observer(topo)
        self.predictor = Predictor(topo, model)
        self.rca = RootCauseAnalyzer(topo)
        self.planner = RecoveryPlanner(topo)
        self.gateway = next(n for n in topo.names if topo.services[n].kind == "edge")
        self.preds: dict = {}
        self.summary: dict = {}
        self.diagnosis: dict | None = None
        self.logs: list[dict] = []
        for _ in range(warmup):
            self.step(analyse=False)

    def step(self, analyse: bool = True) -> None:
        self.logs = self.sim.step()
        self.obs.ingest(self.sim.tick, self.sim.observe(), self.logs, self.sim.events)
        if not analyse:
            return
        self.preds = self.predictor.predict(self.obs)
        self.summary = Predictor.summary(self.preds, self.gateway)
        self.diagnosis = self.rca.analyze(self.obs, self.preds) if self.incident_signal() else None

    def incident_signal(self) -> bool:
        return (any(self.obs.anomalous.values()) or bool(self.summary.get("failed"))
                or any(p["cascade"] >= ALERT for p in self.preds.values()))

    def plan(self) -> dict | None:
        if not self.diagnosis:
            return None
        return self.planner.plan(self.sim, self.obs, self.diagnosis, self.preds)


class Engine:
    def __init__(self, seed: int = 7, interval: float = 1.0):
        self.topo = Topology()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.interval = interval
        self.speed = 1.0
        self.paused = False
        self.autopilot = False
        self.seq = 0
        self.running = False
        self.reset(seed)

    # ------------------------------------------------------------ lifecycle
    def reset(self, seed: int | None = None) -> None:
        with self.lock:
            self.seed = seed if seed is not None else int(time.time()) % 100000
            self.p = Pipeline(self.topo, self.seed)
            self.plan: dict | None = None
            self.plan_tick = -99
            self.avail_hist: deque = deque(maxlen=300)
            self.risk_hist = {n: deque(maxlen=120) for n in self.topo.names}
            self.timeline: deque = deque(maxlen=80)
            self.incidents: list[dict] = []
            self.incident: dict | None = None
            self.calm = 0
            self.last_auto = -99
            self.new_logs: list[dict] = []
            self.p.step()
            self._record()
            self.timeline.append({"tick": self.p.sim.tick, "type": "info", "text": f"Session started (seed {self.seed})"})
            self._publish()

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while self.running:
            t0 = time.time()
            if not self.paused:
                self.step()
            delay = self.interval / max(self.speed, 0.1) - (time.time() - t0)
            time.sleep(max(0.02, delay))

    # ------------------------------------------------------------------ tick
    def step(self) -> None:
        with self.lock:
            p = self.p
            p.step()
            self._record()
            self._track_incident()
            if p.diagnosis and (p.sim.tick - self.plan_tick >= PLAN_EVERY or self.plan is None):
                self.plan = p.plan()
                self.plan_tick = p.sim.tick
            elif not p.diagnosis:
                self.plan = None
            if self.autopilot:
                self._autopilot()
            self._publish()

    def _record(self) -> None:
        p = self.p
        self.avail_hist.append([p.sim.tick, round(p.sim.availability, 4)])
        for n in self.topo.names:
            pr = p.preds.get(n)
            self.risk_hist[n].append(round(pr["cascade"], 3) if pr else 0.0)
        for e in p.sim.events:
            if e["tick"] == p.sim.tick and e["type"] == "crash":
                self.timeline.append({"tick": e["tick"], "type": "crash", "service": e["service"],
                                      "text": f"{e['service']} crashed"})
        self.new_logs = p.logs

    def _track_incident(self) -> None:
        p = self.p
        tick = p.sim.tick
        signal = p.incident_signal()
        failed = p.summary.get("failed", [])
        if self.incident is None:
            if signal:
                self.incident = {"id": len(self.incidents) + 1, "opened": tick, "closed": None,
                                 "root": None, "kind": None, "label": None, "first_prediction": None,
                                 "first_failure": None, "min_availability": 1.0, "peak_failed": 0,
                                 "actions": [], "predicted_services": [], "affected": [], "loss_seconds": 0.0,
                                 "diagnosis": None, "flagged": {}, "service_leads": {}}
                self.incidents.append(self.incident)
                self.calm = 0
                self.timeline.append({"tick": tick, "type": "incident",
                                      "text": f"Incident #{self.incident['id']} opened"})
            else:
                return
        inc = self.incident
        risky = [n for n, pr in p.preds.items() if not pr["failed"] and pr["cascade"] >= ALERT]
        if risky and inc["first_prediction"] is None:
            inc["first_prediction"] = tick
        for n in risky:
            if n not in inc["predicted_services"]:
                inc["predicted_services"].append(n)
            inc["flagged"].setdefault(n, tick)
        if failed and inc["first_failure"] is None:
            inc["first_failure"] = tick
        for n in failed:
            if n not in inc["service_leads"]:
                inc["service_leads"][n] = [inc["flagged"].get(n), tick]
        for n in self.topo.names:
            if (p.obs.anomalous[n] or n in failed) and n not in inc["affected"]:
                inc["affected"].append(n)
        inc["loss_seconds"] += (1.0 - p.sim.availability) * p.sim.tick_seconds
        inc["min_availability"] = min(inc["min_availability"], round(p.sim.availability, 4))
        inc["peak_failed"] = max(inc["peak_failed"], len(failed))
        if p.diagnosis and not inc["actions"]:  # frozen once someone acted on it
            d = p.diagnosis
            inc["root"], inc["kind"], inc["label"] = d["root"], d["kind"], d["label"]
            inc["confidence"] = d["confidence"]
            inc["diagnosis"] = {k: d[k] for k in ("root", "kind", "label", "confidence", "evidence", "ranking")}
        healthy = not signal and p.sim.availability > 0.97
        self.calm = self.calm + 1 if healthy else 0
        if self.calm >= 10:
            inc["closed"] = tick
            inc["duration"] = tick - inc["opened"]
            self.timeline.append({"tick": tick, "type": "resolved",
                                  "text": f"Incident #{inc['id']} resolved after {inc['duration']} ticks"})
            if inc["root"] in inc["affected"]:
                inc["affected"].remove(inc["root"])
            self.incident = None

    def _autopilot(self) -> None:
        p, plan = self.p, self.plan
        if not plan or not p.diagnosis or p.sim.tick - self.last_auto < 20 or p.sim.scheduled:
            return
        history = [{"tick": e["tick"], "kind": e["action"], "target": e["service"]}
                   for e in p.sim.events if e["type"] == "action"]
        best, escalation = should_act(plan, p.diagnosis, p.summary, ALERT, history, p.sim.tick)
        if escalation and self.incident and not self.incident.get("escalated"):
            self.incident["escalated"] = p.sim.tick
            self.timeline.append({"tick": p.sim.tick, "type": "escalation", "text": f"AUTOPILOT: {escalation}"})
        if best:
            self._apply(best["actions"], source="autopilot", label=best["label"], strategy=best)
            self.last_auto = p.sim.tick

    # ------------------------------------------------------------- commands
    def _apply(self, actions: list[dict], source: str, label: str, strategy: dict | None = None) -> None:
        p = self.p
        for a in actions:
            p.sim.apply_action(a["kind"], a["target"], **(a.get("params") or {}))
        self.timeline.append({"tick": p.sim.tick, "type": "action", "source": source,
                              "text": f"{'AUTOPILOT' if source == 'autopilot' else 'Operator'}: {label}"})
        if self.incident:
            entry = {"tick": p.sim.tick, "label": label, "source": source}
            if strategy and self.plan:
                base = next(x for x in self.plan["strategies"] if x["id"] == self.plan["baseline"])
                entry["projected"], entry["baseline"] = strategy["availability"], base["availability"]
            self.incident["actions"].append(entry)
        self.plan_tick = -99  # re-plan on the next tick

    def inject(self, kind: str, target: str, severity: float = 1.0) -> dict:
        with self.lock:
            f = self.p.sim.inject(kind, target, severity)
            self.timeline.append({"tick": self.p.sim.tick, "type": "chaos",
                                  "text": f"Chaos: {FAULT_CATALOG[kind]['label']} on {target}"})
            self._publish()
            return {"ok": True, "fault": f.id}

    def preset(self, preset_id: str) -> dict:
        preset = next((x for x in PRESETS if x["id"] == preset_id), None)
        if not preset:
            raise ValueError(f"unknown scenario {preset_id}")
        with self.lock:
            for kind, target, sev in preset["faults"]:
                self.p.sim.inject(kind, target, sev)
            self.timeline.append({"tick": self.p.sim.tick, "type": "chaos", "text": f"Scenario: {preset['label']}"})
            self._publish()
        return {"ok": True}

    def apply_strategy(self, strategy_id: int) -> dict:
        with self.lock:
            if not self.plan:
                raise ValueError("no active recovery plan")
            s = next((x for x in self.plan["strategies"] if x["id"] == strategy_id), None)
            if s is None:
                raise ValueError("unknown strategy")
            if not s["actions"]:
                return {"ok": True, "noop": True}
            self._apply(s["actions"], source="operator", label=s["label"], strategy=s)
            self._publish()
            return {"ok": True}

    def apply_action(self, kind: str, target: str) -> dict:
        with self.lock:
            self._apply([{"kind": kind, "target": target}], "operator",
                        f"{ACTION_CATALOG[kind]['label']} {target}")
            self._publish()
            return {"ok": True}

    def control(self, body: dict) -> dict:
        with self.lock:
            if "paused" in body:
                self.paused = bool(body["paused"])
            if "speed" in body:
                self.speed = max(0.25, min(8.0, float(body["speed"])))
            if "autopilot" in body:
                self.autopilot = bool(body["autopilot"])
                self.timeline.append({"tick": self.p.sim.tick, "type": "info",
                                      "text": f"Autopilot {'enabled' if self.autopilot else 'disabled'}"})
            if body.get("step"):
                self.step()
            self._publish()
        if body.get("reset"):
            self.reset(body.get("seed"))
        return {"ok": True}

    # ------------------------------------------------------------ snapshots
    def meta(self) -> dict:
        topo = self.topo
        return {
            "topology": topo.to_json(),
            "faults": [{"id": k, **v, "targets": fault_targets(topo, k)} for k, v in FAULT_CATALOG.items()],
            "actions": [{"id": k, **v, "targets": action_targets(topo, k)} for k, v in ACTION_CATALOG.items()],
            "presets": [{"id": x["id"], "label": x["label"]} for x in PRESETS],
            "model": getattr(self.p.predictor.model, "meta", {}),
            "tick_seconds": self.p.sim.tick_seconds,
            "alert": ALERT,
        }

    def _publish(self) -> None:
        self.seq += 1
        self.snapshot = self._build_snapshot()
        self.cond.notify_all()

    def _build_snapshot(self) -> dict:
        p = self.p
        sim, obs = p.sim, p.obs
        services = {}
        for n in self.topo.names:
            m = obs.latest(n)
            pr = p.preds.get(n, {})
            services[n] = {
                "status": obs.status(n),
                "rps": round(m["rps"], 1), "err": round(m["err"], 4), "lat": round(m["lat"], 1),
                "cpu": round(m["cpu"], 3), "mem": round(m["mem"], 3), "sat": round(min(m["sat"], 9.99), 3),
                "disk": round(m["disk"], 3) if self.topo.services[n].has_disk else None,
                "hit": round(m["hit"], 3) if m.get("hit") is not None else None,
                "replicas": m["replicas"], "pending": m["pending"], "breaker": m["breaker"],
                "version": m["version"], "crashes": m["crashes"],
                "anomaly": round(obs.score[n], 2), "anomalous": obs.anomalous[n], "onset": obs.onset[n],
                "risk": round(pr.get("risk", 0.0), 3), "cascade": round(pr.get("cascade", 0.0), 3),
                "eta": None if pr.get("eta") is None else round(pr["eta"], 1),
                "via": pr.get("via"), "drivers": pr.get("drivers", []),
                "zs": {k: round(v, 1) for k, v in obs.zs[n].items() if v >= 3},
            }
        avail = sim.availability
        n_failed = len(p.summary.get("failed", []))
        if avail < 0.5:
            status = "outage"
        elif n_failed or avail < 0.95:
            status = "critical"
        elif p.incident_signal():
            status = "degraded"
        else:
            status = "nominal"
        snap = {
            "seq": self.seq,
            "tick": sim.tick,
            "ts": sim.start_ts + sim.tick * sim.tick_seconds,
            "paused": self.paused, "speed": self.speed, "autopilot": self.autopilot,
            "status": status,
            "availability": round(avail, 4),
            "ingress": round(sim.ingress, 1),
            "rate_limit": sim.rate_limit,
            "services": services,
            "summary": p.summary,
            "rca": p.diagnosis,
            "plan": self.plan,
            "incident": self.incident,
            "incidents": self.incidents[-8:],
            "timeline": list(self.timeline)[-40:],
            "history": list(self.avail_hist),
            "logs": self.new_logs,
            "pending_actions": [{"op": s["op"], "target": s["target"], "due": s["due"]} for s in sim.scheduled],
            "chaos": [{"kind": f.kind, "target": f.target, "since": f.start} for f in sim.faults],
        }
        snap["briefing"] = report.briefing(snap, sim.tick_seconds)
        return snap

    def postmortem(self, incident_id: int | None = None) -> dict:
        with self.lock:
            if not self.incidents:
                raise ValueError("no incidents yet")
            inc = self.incidents[-1] if incident_id is None else next(
                (i for i in self.incidents if i["id"] == incident_id), None)
            if inc is None:
                raise ValueError(f"unknown incident {incident_id}")
            md = report.postmortem(inc, list(self.timeline), self.p.sim.tick_seconds, self.p.sim.tick)
            return {"id": inc["id"], "markdown": md}

    def full_state(self) -> dict:
        with self.lock:
            snap = dict(self.snapshot)
            snap["risk_history"] = {n: list(v) for n, v in self.risk_hist.items()}
            snap["metric_history"] = {
                n: {k: [round(x[k], 4) if isinstance(x[k], float) else x[k] for x in self.p.obs.hist[n]]
                    for k in ("err", "lat", "cpu", "mem", "sat")}
                for n in self.topo.names
            }
            snap["log_tail"] = [l for l in list(self.p.obs.recent_logs)[-150:]]
            return snap
