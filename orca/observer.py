"""Telemetry ingestion: metric history, adaptive baselines, anomaly detection,
log analytics and feature extraction for the failure predictor.

The observer only sees what an SRE would see: metrics, raw log lines and
change events (deploys, remediation actions, container crashes). It never
looks at the simulator's injected ground truth.
"""
from __future__ import annotations

import math
from collections import Counter, deque

from .logparse import parse
from .topology import Topology

# metric -> (transform, std floor, direction) ; direction +1 = only increases are bad
METRICS = {
    "err": (lambda v: v, 0.01, 1),
    "lat": (lambda v: math.log(max(v, 0.5)), 0.08, 1),
    "cpu": (lambda v: v, 0.03, 1),
    "mem": (lambda v: v, 0.02, 1),
    "sat": (lambda v: v, 0.04, 1),
    "rps": (lambda v: math.log(max(v, 0.1)), 0.06, 0),
    "disk": (lambda v: v, 0.01, 1),
    "hit": (lambda v: v, 0.02, -1),
}
FEATURES = [
    "err", "err_slope", "lat_ratio_log", "lat_slope", "cpu", "cpu_slope", "mem", "mem_slope",
    "mem_ttf", "sat", "sat_slope", "disk_ttf", "rps_ratio_log", "hit_drop", "err_logs",
    "intrinsic_logs", "dep_err_max", "dep_sat_max", "dep_lat_max", "dep_anomaly", "anomaly",
    "recent_crash",
]
ANOMALY_THRESHOLD = 4.0
WARMUP = 25


def slope(values: list[float]) -> float:
    """Least-squares slope per tick."""
    n = len(values)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(values) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(values))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den


def time_to(level: float, threshold: float, rate: float, horizon: float = 120.0) -> float:
    """Ticks until `level` reaches `threshold` at `rate` per tick (inf if not rising)."""
    if level >= threshold:
        return 0.0
    if rate <= 1e-5:
        return math.inf
    t = (threshold - level) / rate
    return t if t <= horizon else math.inf


class Baseline:
    """Exponentially weighted mean/variance that stops learning while anomalous."""

    def __init__(self, floor: float):
        self.mean = None
        self.var = 0.0
        self.n = 0
        self.floor = floor

    def z(self, x: float) -> float:
        if self.mean is None:
            return 0.0
        return (x - self.mean) / max(math.sqrt(self.var), self.floor)

    def update(self, x: float, warm: bool) -> None:
        if self.mean is None:
            self.mean, self.n = x, 1
            return
        alpha = max(1.0 / (self.n + 1), 0.15) if not warm else 0.01
        if warm and abs(self.z(x)) > 2.5:
            return
        d = x - self.mean
        self.mean += alpha * d
        self.var = (1 - alpha) * (self.var + alpha * d * d)
        self.n += 1


class Observer:
    def __init__(self, topo: Topology, window: int = 40):
        self.topo = topo
        self.tick = 0
        self.window = window
        self.hist: dict[str, deque] = {n: deque(maxlen=window) for n in topo.names}
        self.base: dict[str, dict[str, Baseline]] = {
            n: {m: Baseline(spec[1]) for m, spec in METRICS.items()} for n in topo.names
        }
        self.zs: dict[str, dict[str, float]] = {n: {} for n in topo.names}
        self.score: dict[str, float] = {n: 0.0 for n in topo.names}
        self.score_hist: dict[str, deque] = {n: deque(maxlen=window) for n in topo.names}
        self.streak: dict[str, int] = {n: 0 for n in topo.names}
        self.calm: dict[str, int] = {n: 0 for n in topo.names}
        self.onset: dict[str, int | None] = {n: None for n in topo.names}
        self.anomalous: dict[str, bool] = {n: False for n in topo.names}
        self.log_hist: dict[str, deque] = {n: deque(maxlen=12) for n in topo.names}
        self.blame_hist: deque = deque(maxlen=12)   # per tick Counter[(caller, callee)]
        self.events: list[dict] = []
        self.recent_logs: deque = deque(maxlen=400)
        self.template_counts: Counter = Counter()

    # --------------------------------------------------------------- ingest
    def ingest(self, tick: int, metrics: dict[str, dict], logs: list[dict], events: list[dict]) -> None:
        self.tick = tick
        self.events = events
        per_service: dict[str, Counter] = {n: Counter() for n in self.topo.names}
        blame: Counter = Counter()
        for raw in logs:
            p = parse(raw)
            self.recent_logs.append({**raw, "template": p.template, "category": p.category,
                                     "intrinsic": p.intrinsic, "blame": p.blame})
            self.template_counts[p.template] += 1
            c = per_service.get(p.service)
            if c is None:
                continue
            c["lines"] += 1
            if p.level in ("ERROR", "FATAL"):
                c["errors"] += 1
            if p.intrinsic:
                c["intrinsic"] += 1
                c["cat:" + p.category] += 1
            c["tpl:" + p.template] += 1
            if p.blame and p.blame in per_service:
                blame[(p.service, p.blame)] += 1
        self.blame_hist.append(blame)

        warm = tick > WARMUP
        # never learn "normal" while something is wrong: slow drift during a long incident
        # would otherwise make an overloaded database look merely "slow at normal load"
        frozen = warm and any(self.anomalous.values())
        for n in self.topo.names:
            m = metrics[n]
            self.log_hist[n].append(per_service[n])
            self.hist[n].append({**m, "tick": tick})
            zs = {}
            if not m["down"]:
                for name, (fn, _floor, direction) in METRICS.items():
                    v = m.get(name)
                    if v is None or (name == "disk" and not self.topo.services[n].has_disk):
                        continue
                    b = self.base[n][name]
                    z = b.z(fn(v))
                    zs[name] = z if direction == 0 else max(0.0, direction * z)
                    if not frozen:
                        b.update(fn(v), warm)
                if zs.get("rps", 0) < 0:
                    zs["rps"] = abs(zs["rps"]) * 0.5  # traffic drops matter less than spikes
            self.zs[n] = zs
            log_score = min(8.0, sum(c["errors"] for c in list(self.log_hist[n])[-3:]) * 0.8)
            score = 20.0 if m["down"] else max([min(z, 20.0) for z in zs.values()] + [log_score])
            if not warm:
                score = 0.0
            self.score[n] = score
            self.score_hist[n].append(score)
            if score >= ANOMALY_THRESHOLD:
                self.streak[n] += 1
                self.calm[n] = 0
                if self.onset[n] is None:
                    self.onset[n] = tick
            else:
                self.streak[n] = 0
                self.calm[n] += 1
                if self.calm[n] >= 8:
                    self.onset[n] = None
            if self.streak[n] >= 2:
                self.anomalous[n] = True
            elif self.calm[n] >= 4:
                self.anomalous[n] = False

    # -------------------------------------------------------------- helpers
    def latest(self, n: str) -> dict:
        return self.hist[n][-1]

    def series(self, n: str, metric: str, k: int | None = None) -> list[float]:
        h = list(self.hist[n])
        if k:
            h = h[-k:]
        return [x[metric] for x in h if x.get(metric) is not None]

    def baseline_value(self, n: str, metric: str) -> float | None:
        b = self.base[n][metric]
        if b.mean is None:
            return None
        if metric in ("lat", "rps"):
            return math.exp(b.mean)
        return b.mean

    def up_series(self, n: str, metric: str, k: int) -> list[float]:
        """Recent values while the service was up (crash periods distort trends)."""
        out = []
        for x in reversed(self.hist[n]):
            if x["down"]:
                break
            out.append(x[metric])
            if len(out) >= k:
                break
        return out[::-1]

    def log_count(self, n: str, key: str, k: int = 10) -> int:
        return sum(c[key] for c in list(self.log_hist[n])[-k:])

    def blame_counts(self, k: int = 10) -> Counter:
        total: Counter = Counter()
        for c in list(self.blame_hist)[-k:]:
            total.update(c)
        return total

    def recent_crashes(self, n: str, k: int = 30) -> int:
        return sum(1 for e in self.events if e["type"] == "crash" and e["service"] == n
                   and e["tick"] > self.tick - k)

    def is_failed(self, n: str) -> bool:
        m = self.latest(n)
        return m["down"] or m["err"] >= 0.35

    def status(self, n: str) -> str:
        m = self.latest(n)
        if m["down"]:
            return "down"
        if m["err"] >= 0.35:
            return "failed"
        base_lat = self.baseline_value(n, "lat") or m["lat"]
        ratio = m["lat"] / max(base_lat, 0.5)
        if m["err"] >= 0.12 or m["sat"] >= 0.95 or m["mem"] >= 0.92 or ratio >= 5 or m["disk"] >= 0.95:
            return "critical"
        if (m["err"] >= 0.03 or m["sat"] >= 0.75 or m["mem"] >= 0.82 or ratio >= 2.2
                or m["disk"] >= 0.85 or self.anomalous[n]):
            return "degraded"
        return "healthy"

    # ------------------------------------------------------------- features
    def features(self, n: str) -> dict[str, float]:
        m = self.latest(n)
        spec = self.topo.services[n]
        k = 6
        err_s = self.series(n, "err", k)
        lat_s = [math.log(max(v, 0.5)) for v in self.up_series(n, "lat", k)]
        cpu_s = self.up_series(n, "cpu", k)
        mem_s = self.up_series(n, "mem", 10)
        sat_s = self.up_series(n, "sat", k)
        disk_s = self.up_series(n, "disk", 10)
        base_lat = self.baseline_value(n, "lat") or max(m["lat"], 0.5)
        base_rps = self.baseline_value(n, "rps") or max(m["rps"], 0.1)
        mem_rate = slope(mem_s)
        disk_rate = slope(disk_s)
        mem_ttf = time_to(m["mem"], 1.0, mem_rate)
        disk_ttf = time_to(m["disk"], 0.98, disk_rate) if spec.has_disk else math.inf
        hit = m.get("hit")
        base_hit = self.baseline_value(n, "hit") if hit is not None else None

        dep_err = dep_sat = dep_lat = dep_anom = 0.0
        for d in self.topo.callees[n]:
            dm = self.latest(d.callee)
            w = 1.0 if d.hard else 0.3
            e = 1.0 if dm["down"] else dm["err"]
            dep_err = max(dep_err, w * e)
            dep_sat = max(dep_sat, w * min(dm["sat"], 3.0))
            dbase = self.baseline_value(d.callee, "lat") or max(dm["lat"], 0.5)
            dep_lat = max(dep_lat, w * math.log(max(dm["lat"], 0.5) / max(dbase, 0.5)))
            dep_anom = max(dep_anom, w * min(self.score[d.callee], 20.0) / 10.0)

        def decay(ttf: float) -> float:
            return 0.0 if math.isinf(ttf) else math.exp(-ttf / 15.0)

        return {
            "err": 1.0 if m["down"] else m["err"],
            "err_slope": slope(err_s) * 10,
            "lat_ratio_log": math.log(max(m["lat"], 0.5) / max(base_lat, 0.5)),
            "lat_slope": slope(lat_s) * 10,
            "cpu": m["cpu"],
            "cpu_slope": slope(cpu_s) * 10,
            "mem": m["mem"],
            "mem_slope": mem_rate * 100,
            "mem_ttf": decay(mem_ttf),
            "sat": min(m["sat"], 3.0),
            "sat_slope": slope(sat_s) * 10,
            "disk_ttf": decay(disk_ttf),
            "rps_ratio_log": math.log(max(m["rps"], 0.1) / max(base_rps, 0.1)),
            "hit_drop": max(0.0, (base_hit or 0) - hit) * 3 if hit is not None else 0.0,
            "err_logs": min(6.0, self.log_count(n, "errors", 3) / 3.0),
            "intrinsic_logs": min(6.0, self.log_count(n, "intrinsic", 5) / 2.0),
            "dep_err_max": dep_err,
            "dep_sat_max": dep_sat,
            "dep_lat_max": dep_lat,
            "dep_anomaly": dep_anom,
            "anomaly": min(self.score[n], 20.0) / 10.0,
            "recent_crash": min(3, self.recent_crashes(n)) / 3.0,
        }

    def resource_eta(self, n: str) -> float | None:
        """Ticks until memory or disk is exhausted, if a steady trend says so."""
        m = self.latest(n)
        if m["down"]:
            return None
        etas = []
        mem_s = self.up_series(n, "mem", 10)
        if len(mem_s) >= 8 and m["mem"] > 0.6:
            etas.append(time_to(m["mem"], 1.0, slope(mem_s)))
        if self.topo.services[n].has_disk:
            disk_s = self.up_series(n, "disk", 10)
            if len(disk_s) >= 8 and m["disk"] > 0.7:
                etas.append(time_to(m["disk"], 0.98, slope(disk_s)))
        eta = min(etas, default=math.inf)
        return None if math.isinf(eta) else max(1.0, eta)

    def trend_eta(self, n: str) -> float | None:
        """Ticks until a hard failure threshold is crossed, extrapolating current trends."""
        m = self.latest(n)
        if m["down"] or m["err"] >= 0.35:
            return 0.0
        etas = [
            time_to(m["err"], 0.35, slope(self.series(n, "err", 6))),
            time_to(m["mem"], 1.0, slope(self.up_series(n, "mem", 10))),
            time_to(m["sat"], 1.55, slope(self.up_series(n, "sat", 6))),
        ]
        if self.topo.services[n].has_disk:
            etas.append(time_to(m["disk"], 0.98, slope(self.up_series(n, "disk", 10))))
        eta = min(etas)
        return None if math.isinf(eta) else max(1.0, eta)
