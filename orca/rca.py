"""Root-cause analysis.

Symptoms flow *up* the dependency graph (callers suffer when callees break),
so the analyser walks *down* it, from symptoms towards causes:

* **Candidate graph** - anomalous services plus any service whose logs or
  metrics show an intrinsic fault signature.
* **Random walk with restart** (personalised PageRank, as in MicroRCA):
  from every node the walker moves to an anomalous dependency with a weight
  given by log *blame pointers* (``Timeout ... from product-db``) and the
  correlation of the two anomaly time series, or stays put in proportion to
  the node's *intrinsic* evidence (OOM, slow queries, exceptions after a
  deploy, disk full, ingress surge...). The stationary distribution
  concentrates on the services that explain everyone else's symptoms.
* **Temporal and change priors** - earlier anomaly onset and a deploy shortly
  before onset raise the score.
* **Fault classification** - signature rules over metrics, log templates and
  change events label the top candidate with a fault type, and every
  conclusion carries human-readable evidence.
"""
from __future__ import annotations

import math

from .observer import Observer, slope
from .topology import Topology

FAULT_LABELS = {
    "memory_leak": "Memory leak",
    "cache_crash": "Cache memory exhaustion",
    "db_slow_queries": "Database lock contention / slow queries",
    "disk_fill": "Disk exhaustion",
    "bad_deploy": "Faulty deployment",
    "traffic_surge": "Ingress traffic surge",
    "overload": "Capacity exhaustion",
}


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 4:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va < 1e-9 or vb < 1e-9:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


class RootCauseAnalyzer:
    def __init__(self, topo: Topology):
        self.topo = topo
        self.gateway = next(n for n in topo.names if topo.services[n].kind == "edge")

    # -------------------------------------------------------------- evidence
    def signatures(self, obs: Observer, n: str) -> list[tuple[str, float, list[str]]]:
        """Fault-type hypotheses for one service: (kind, strength, evidence)."""
        spec = self.topo.services[n]
        m = obs.latest(n)
        out = []
        mem_s = obs.up_series(n, "mem", 12)
        mem_rate = slope(mem_s)
        mem_peak = max(obs.series(n, "mem", 20) or [0])
        oom = obs.log_count(n, "tpl:oom_killed", 12) + obs.log_count(n, "tpl:oom_error", 12)
        gc = obs.log_count(n, "tpl:gc_pause", 12)
        crashes = [e for e in obs.events if e["type"] == "crash" and e["service"] == n
                   and e["tick"] > obs.tick - 40]
        oom_crashes = [e for e in crashes if "OOMKilled" in e.get("reason", "")]
        deploys = [e for e in obs.events if e["type"] == "deploy" and e["service"] == n
                   and not e.get("rollback") and e["tick"] > obs.tick - 90]
        onset = obs.onset[n]

        if spec.kind == "cache":
            hit = m.get("hit")
            ev, s = [], 0.0
            if mem_rate > 0.002 and m["mem"] > 0.6:
                s += min(2.0, mem_rate * 250)
                ev.append(f"memory climbing {mem_rate * 100:.1f}%/tick (now {m['mem'] * 100:.0f}% of maxmemory)")
            ev_count = obs.log_count(n, "tpl:cache_evictions", 10)
            if ev_count:
                s += min(1.5, ev_count * 0.2)
                ev.append(f"{ev_count} eviction-storm log lines")
            if hit is not None and hit < 0.75:
                s += 1.0
                ev.append(f"hit ratio collapsed to {hit * 100:.0f}% (baseline 85%)")
            if m["down"] or oom_crashes:
                s += 1.0
                ev.append("cache process crashed")
            if s:
                out.append(("cache_crash", s, ev))
        elif not spec.stateful and (mem_rate > 0.003 and m["mem"] > 0.6 or oom_crashes or mem_peak > 0.95):
            ev = []
            s = 0.5
            if mem_rate > 0.003:
                s += min(2.0, mem_rate * 250)
                ev.append(f"heap growing {mem_rate * 100:.1f}%/tick independent of load (now {m['mem'] * 100:.0f}%)")
            if oom or oom_crashes:
                s += 1.0
                ev.append(f"{len(oom_crashes)} OOMKilled restarts, {oom} OutOfMemoryError lines")
            if gc:
                s += 0.3
                ev.append(f"{gc} long GC pauses")
            if deploys:
                s += 0.6
                d = deploys[-1]
                ev.append(f"release {d['version']} deployed at t={d['tick']} (leak starts after it)")
            out.append(("memory_leak", s, ev))

        if spec.kind == "db":
            slow = obs.log_count(n, "tpl:slow_query", 10)
            base_lat = obs.baseline_value(n, "lat") or m["lat"]
            ratio = m["lat"] / max(base_lat, 0.5)
            rps_ratio = m["rps"] / max(obs.baseline_value(n, "rps") or m["rps"], 0.1)
            # a database that slows down while its load is normal is contended, not overloaded
            if slow or (ratio > 1.4 and rps_ratio < 1.3):
                s = min(2.0, slow * 0.25) + (min(1.5, 0.4 + (ratio - 1.0) * 0.4) if rps_ratio < 1.3 else 0.0)
                ev = [f"query latency {ratio:.0f}x baseline while load is only {rps_ratio:.1f}x" if ratio >= 10 else
                      f"query latency {ratio:.1f}x baseline while load is only {rps_ratio:.1f}x"]
                if slow:
                    ev.append(f"{slow} slow-query / lock-wait log lines")
                out.append(("db_slow_queries", s, ev))

        if spec.has_disk:
            disk_rate = slope(obs.up_series(n, "disk", 12))
            full = obs.log_count(n, "tpl:disk_full", 10)
            high = obs.log_count(n, "tpl:disk_high", 10)
            # healthy volumes are flat; steady growth is conclusive long before the disk is full
            if m["disk"] > 0.85 or full or disk_rate > 0.003:
                s = 1.0 + min(1.5, full * 0.3) + min(0.5, high * 0.1) + min(1.5, disk_rate * 200)
                eta = (0.98 - m["disk"]) / disk_rate if disk_rate > 1e-4 else None
                ev = [f"disk at {m['disk'] * 100:.0f}% and growing {disk_rate * 100:.2f}%/tick"
                      + (f" - full in ~{eta:.0f} ticks" if eta is not None and eta > 0 else "")]
                if full:
                    ev.append(f"{full} 'No space left on device' write failures")
                out.append(("disk_fill", s, ev))

        exc = obs.log_count(n, "tpl:exception", 12)
        if exc >= 2:
            s = min(2.5, exc * 0.15)
            ev = [f"{exc} unhandled exceptions in own code"]
            if deploys:
                d = deploys[-1]
                lag = (onset - d["tick"]) if onset is not None else None
                s += 1.0
                ev.append(f"release {d['version']} deployed at t={d['tick']}"
                          + (f", {lag} ticks before anomaly onset" if lag is not None and lag >= 0 else ""))
            out.append(("bad_deploy", s, ev))

        if spec.kind == "edge":
            base = obs.baseline_value(n, "rps") or m["rps"]
            ratio = m["rps"] / max(base, 0.1)
            growth = slope([v / max(base, 0.1) for v in obs.series(n, "rps", 8)])
            # level or sustained ramp (diurnal drift is < 0.002/tick)
            if ratio > 1.12 or (ratio > 1.06 and growth > 0.004):
                s = min(3.0, (ratio - 1.0) * 4.0 + max(0.0, growth) * 120)
                ev = [f"ingress {m['rps']:.0f} req/s = {ratio:.2f}x learned baseline ({base:.0f} req/s)"]
                if growth > 0.004:
                    ev.append(f"ingress still ramping +{growth * 100:.1f}% of baseline per tick")
                out.append(("traffic_surge", s, ev))
        return out

    # --------------------------------------------------------------- analyse
    def analyze(self, obs: Observer, preds: dict | None = None) -> dict | None:
        topo = self.topo
        anomalous = [n for n in topo.names if obs.anomalous[n] or obs.is_failed(n)]
        sigs = {n: self.signatures(obs, n) for n in topo.names}
        intrinsic = {n: max([s for _, s, _ in sigs[n]] + [0.0]) for n in topo.names}
        nodes = sorted(set(anomalous) | {n for n in topo.names if intrinsic[n] >= 1.0})
        if not nodes:
            return None
        idx = {n: i for i, n in enumerate(nodes)}
        blame = obs.blame_counts(10)
        node_set = set(nodes)

        # transition matrix (row-stochastic)
        P = [[0.0] * len(nodes) for _ in nodes]
        for a in nodes:
            row = P[idx[a]]
            deps = [d for d in topo.callees[a] if d.callee in node_set]
            blame_total = sum(blame[(a, d.callee)] for d in deps) or 1
            for d in deps:
                b = d.callee
                c = max(0.0, _corr(list(obs.score_hist[a]), list(obs.score_hist[b])))
                w = 0.15 + 1.2 * blame[(a, b)] / blame_total + 0.6 * c
                if d.hard:
                    w *= 1.3
                row[idx[b]] += w
            for d in topo.callers[a]:
                if d.caller in node_set:
                    row[idx[d.caller]] += 0.05
            row[idx[a]] += 0.2 + 1.5 * intrinsic[a] + (0.4 if not deps else 0.0)
            total = sum(row)
            for j in range(len(row)):
                row[j] /= total

        pers = [0.2 + min(obs.score[n], 20.0) / 20.0 + 0.3 * intrinsic[n] for n in nodes]
        ps = sum(pers)
        pers = [p / ps for p in pers]
        x = pers[:]
        alpha = 0.85
        for _ in range(60):
            nx = [(1 - alpha) * p for p in pers]
            for i, xi in enumerate(x):
                if xi == 0.0:
                    continue
                for j, pij in enumerate(P[i]):
                    if pij:
                        nx[j] += alpha * xi * pij
            x = nx

        onsets = [obs.onset[n] for n in nodes if obs.onset[n] is not None]
        first = min(onsets) if onsets else obs.tick
        scores = {}
        for n in nodes:
            on = obs.onset[n]
            early = 1.0 / (1.0 + max(0, (on if on is not None else obs.tick) - first) / 4.0)
            scores[n] = x[idx[n]] * (1.0 + 0.5 * early) * (1.0 + 0.25 * min(intrinsic[n], 3.0))
        total = sum(scores.values()) or 1.0
        ranking = sorted(nodes, key=lambda n: -scores[n])

        root = ranking[0]
        kind, strength, evidence = self._classify(obs, root, sigs[root])
        conf = scores[root] / total
        # sharpen: a clear signature and a clear ranking gap both raise confidence
        gap = (scores[root] - scores[ranking[1]]) / total if len(ranking) > 1 else 1.0
        confidence = max(0.05, min(0.99, 0.35 * conf + 0.35 * min(1.0, gap * 3) + 0.3 * min(1.0, strength / 2.0)))

        blamed_by = sorted({a for (a, b), c in blame.items() if b == root and c > 0})
        evidence = list(evidence)
        if blamed_by:
            n_lines = sum(c for (a, b), c in blame.items() if b == root)
            evidence.append(f"{n_lines} dependency-error log lines from {', '.join(blamed_by)} point at {root}")
        if obs.onset[root] is not None and onsets:
            order = sorted(set(onsets))
            rank = order.index(obs.onset[root]) + 1
            evidence.append(f"anomaly onset t={obs.onset[root]} (#{rank} of {len(anomalous)} anomalous services)")
        healthy_deps = [d.callee for d in topo.callees[root] if d.callee not in node_set]
        if topo.callees[root] and len(healthy_deps) == len(topo.callees[root]):
            evidence.append("all of its own dependencies are healthy - the fault originates here")

        bottleneck = None
        if kind in ("traffic_surge", "overload"):
            cands = [n for n in anomalous if topo.services[n].kind != "edge"]
            if cands:
                bottleneck = max(cands, key=lambda n: obs.latest(n)["sat"] + obs.latest(n)["cpu"])
                evidence.append(f"tightest bottleneck: {bottleneck} (pool {obs.latest(bottleneck)['sat'] * 100:.0f}%, "
                                f"CPU {obs.latest(bottleneck)['cpu'] * 100:.0f}%)")

        # other strong, independent fault signatures (e.g. a surge on top of DB contention)
        secondary = []
        for n in topo.names:
            for k, s, ev in sigs[n]:
                if s >= 1.0 and not (n == root and k == kind) and k != "overload":
                    secondary.append({"service": n, "kind": k, "strength": round(s, 2), "evidence": ev[:1]})
        secondary.sort(key=lambda x: -x["strength"])
        affected = sorted(set(anomalous) - {root})
        path = self._propagation_path(root, set(anomalous))
        return {
            "root": root,
            "kind": kind,
            "label": FAULT_LABELS.get(kind, kind),
            "confidence": round(confidence, 3),
            "evidence": evidence,
            "bottleneck": bottleneck,
            "ranking": [{"service": n, "score": round(scores[n] / total, 3),
                         "kinds": [k for k, _, _ in sigs[n]]} for n in ranking[:6]],
            "affected": affected,
            "secondary": secondary[:3],
            "propagation": path,
            "onset": obs.onset[root] if obs.onset[root] is not None else first,
        }

    def _classify(self, obs: Observer, n: str, sigs) -> tuple[str, float, list[str]]:
        if sigs:
            kind, s, ev = max(sigs, key=lambda x: x[1])
            return kind, s, ev
        m = obs.latest(n)
        return "overload", 0.6, [f"saturated: pool {m['sat'] * 100:.0f}%, CPU {m['cpu'] * 100:.0f}%, "
                                 f"error rate {m['err'] * 100:.0f}% with no intrinsic fault signature"]

    def _propagation_path(self, root: str, anomalous: set[str]) -> list[list[str]]:
        """Edges (caller, callee) along which the failure is spreading from the root."""
        edges = []
        frontier = [root]
        seen = {root}
        while frontier:
            nxt = []
            for c in frontier:
                for d in self.topo.callers[c]:
                    if d.caller in anomalous:
                        edges.append([d.caller, c])
                        if d.caller not in seen:
                            seen.add(d.caller)
                            nxt.append(d.caller)
            frontier = nxt
        return edges
