"""Failure prediction.

Two layers:

1. **Direct risk** - a logistic-regression model (trained offline on simulated
   incident histories, see ``orca.train``) estimates the probability that a
   service crosses the failure threshold within the next ``HORIZON`` ticks
   from its own telemetry and its dependencies' state. Being linear, every
   prediction decomposes into per-feature contributions, which the dashboard
   shows as "why is this service at risk".
2. **Cascade risk** - direct risks are propagated up the dependency graph with
   a noisy-OR model: a caller fails if it fails on its own *or* because a hard
   dependency fails and takes it down. This captures multi-hop spread beyond
   the model's horizon and yields a blast radius and a cascade ETA.
"""
from __future__ import annotations

import json
import math
import os
import random

from .observer import FEATURES, Observer
from .topology import Topology

HORIZON = 15
RISK_FLOOR = 0.15
ALERT = 0.6  # cascade risk at which a service is flagged "at risk"
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "predictor.json")

FEATURE_LABELS = {
    "err": "error rate", "err_slope": "error rate rising", "lat_ratio_log": "latency vs baseline",
    "lat_slope": "latency rising", "cpu": "CPU", "cpu_slope": "CPU rising", "mem": "memory",
    "mem_slope": "memory growth", "mem_ttf": "memory exhaustion imminent", "sat": "worker/conn pool saturation",
    "sat_slope": "pool saturation rising", "disk_ttf": "disk full imminent", "rps_ratio_log": "traffic vs baseline",
    "hit_drop": "cache hit-ratio drop", "err_logs": "error log rate", "intrinsic_logs": "fault signatures in logs",
    "dep_err_max": "failing hard dependency", "dep_sat_max": "saturated dependency",
    "dep_lat_max": "slow dependency", "dep_anomaly": "anomalous dependency", "anomaly": "anomaly score",
    "recent_crash": "recent crashes",
}


def _sig(x: float) -> float:
    if x < -35:
        return 0.0
    if x > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


class LogisticModel:
    def __init__(self, features=FEATURES, weights=None, bias=0.0, mean=None, std=None, meta=None):
        self.features = list(features)
        k = len(self.features)
        self.w = list(weights) if weights else [0.0] * k
        self.b = bias
        self.mean = list(mean) if mean else [0.0] * k
        self.std = list(std) if std else [1.0] * k
        self.meta = meta or {}

    def _x(self, f: dict) -> list[float]:
        return [(f[name] - mu) / sd for name, mu, sd in zip(self.features, self.mean, self.std)]

    def predict(self, f: dict) -> float:
        x = self._x(f)
        return _sig(self.b + sum(w * v for w, v in zip(self.w, x)))

    def explain(self, f: dict, top: int = 4) -> list[dict]:
        """Largest positive contributions to the log-odds, relative to a healthy baseline."""
        x = self._x(f)
        contrib = [(name, w * v, f[name]) for name, w, v in zip(self.features, self.w, x)]
        contrib.sort(key=lambda c: -c[1])
        return [{"feature": name, "label": FEATURE_LABELS.get(name, name), "contribution": round(c, 2),
                 "value": round(val, 3)} for name, c, val in contrib[:top] if c > 0.25]

    # -------------------------------------------------------------- training
    def fit(self, X: list[dict], y: list[int], epochs: int = 12, lr: float = 0.05, l2: float = 1e-3,
            seed: int = 0, weights: list[float] | None = None) -> None:
        k = len(self.features)
        n = len(X)
        cols = [[row[name] for row in X] for name in self.features]
        self.mean = [sum(c) / n for c in cols]
        self.std = [max(math.sqrt(sum((v - mu) ** 2 for v in c) / n), 1e-6) for c, mu in zip(cols, self.mean)]
        data = [self._x(row) for row in X]
        sw = weights or [1.0] * n
        rng = random.Random(seed)
        idx = list(range(n))
        w, b = [0.0] * k, 0.0
        m_w, v_w, m_b, v_b = [0.0] * k, [0.0] * k, 0.0, 0.0
        beta1, beta2, eps, step = 0.9, 0.999, 1e-8, 0
        batch = 64
        for _ in range(epochs):
            rng.shuffle(idx)
            for s in range(0, n, batch):
                chunk = idx[s:s + batch]
                g_w = [0.0] * k
                g_b = 0.0
                for i in chunk:
                    xi = data[i]
                    p = _sig(b + sum(wj * xj for wj, xj in zip(w, xi)))
                    g = (p - y[i]) * sw[i]
                    for j in range(k):
                        g_w[j] += g * xi[j]
                    g_b += g
                step += 1
                inv = 1.0 / len(chunk)
                for j in range(k):
                    gj = g_w[j] * inv + l2 * w[j]
                    m_w[j] = beta1 * m_w[j] + (1 - beta1) * gj
                    v_w[j] = beta2 * v_w[j] + (1 - beta2) * gj * gj
                    w[j] -= lr * (m_w[j] / (1 - beta1 ** step)) / (math.sqrt(v_w[j] / (1 - beta2 ** step)) + eps)
                gb = g_b * inv
                m_b = beta1 * m_b + (1 - beta1) * gb
                v_b = beta2 * v_b + (1 - beta2) * gb * gb
                b -= lr * (m_b / (1 - beta1 ** step)) / (math.sqrt(v_b / (1 - beta2 ** step)) + eps)
        self.w, self.b = w, b

    def save(self, path: str = MODEL_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"features": self.features, "weights": self.w, "bias": self.b,
                       "mean": self.mean, "std": self.std, "meta": self.meta}, fh, indent=1)

    @classmethod
    def load(cls, path: str = MODEL_PATH) -> "LogisticModel":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return cls(d["features"], d["weights"], d["bias"], d["mean"], d["std"], d.get("meta"))


class GBDTModel:
    """Gradient-boosted decision trees (logistic loss), pure Python.

    Histogram-based split finding on quantile bins. Predictions are explained
    exactly with Saabas path attribution: every split a sample passes through
    credits its feature with the change in node value, so the contributions
    plus the bias sum to the model's log-odds.
    """

    def __init__(self, features=FEATURES, trees=None, base=0.0, meta=None):
        self.features = list(features)
        self.trees = trees or []
        self.base = base
        self.meta = meta or {}

    # node layout: [feature_index, threshold, left, right, value]; leaf: [-1, 0, None, None, value]
    def _margin_and_contrib(self, f: dict):
        x = [f[name] for name in self.features]
        margin = self.base
        contrib = [0.0] * len(self.features)
        for tree in self.trees:
            node = tree
            while node[0] >= 0:
                child = node[2] if x[node[0]] <= node[1] else node[3]
                contrib[node[0]] += child[4] - node[4]
                node = child
            margin += node[4]
        return margin, contrib

    def predict(self, f: dict) -> float:
        return _sig(self._margin_and_contrib(f)[0])

    def explain(self, f: dict, top: int = 4) -> list[dict]:
        _, contrib = self._margin_and_contrib(f)
        items = sorted(zip(self.features, contrib), key=lambda c: -c[1])
        return [{"feature": name, "label": FEATURE_LABELS.get(name, name), "contribution": round(c, 2),
                 "value": round(f[name], 3)} for name, c in items[:top] if c > 0.15]

    def fit(self, X: list[dict], y: list[int], n_trees: int = 140, depth: int = 4, lr: float = 0.12,
            lam: float = 1.0, min_hess: float = 4.0, subsample: float = 0.7, n_bins: int = 32,
            seed: int = 0, weights: list[float] | None = None, log=None) -> None:
        rng = random.Random(seed)
        k = len(self.features)
        n = len(X)
        cols = [[row[name] for row in X] for name in self.features]
        cuts = []
        for c in cols:
            s = sorted(c)
            qs = sorted({s[min(n - 1, int(n * q / n_bins))] for q in range(1, n_bins)})
            cuts.append(qs)
        import bisect
        bins = [[bisect.bisect_left(cuts[j], v) for v in cols[j]] for j in range(k)]
        nb = [len(c) + 1 for c in cuts]
        sw = weights or [1.0] * n
        p0 = min(max(sum(wi * yi for wi, yi in zip(sw, y)) / sum(sw), 1e-3), 1 - 1e-3)
        self.base = math.log(p0 / (1 - p0))
        margin = [self.base] * n
        self.trees = []

        def build(rows, g, h, level):
            G = sum(g[r] for r in rows)
            H = sum(h[r] for r in rows)
            value = -G / (H + lam) * lr
            if level == depth or H < 2 * min_hess or len(rows) < 8:
                return [-1, 0.0, None, None, value]
            best = (0.0, -1, -1)
            parent_score = G * G / (H + lam)
            for j in range(k):
                col = bins[j]
                hg = [0.0] * nb[j]
                hh = [0.0] * nb[j]
                for r in rows:
                    b = col[r]
                    hg[b] += g[r]
                    hh[b] += h[r]
                gl = hl = 0.0
                for b in range(nb[j] - 1):
                    gl += hg[b]
                    hl += hh[b]
                    hr = H - hl
                    if hl < min_hess or hr < min_hess:
                        continue
                    gr = G - gl
                    gain = gl * gl / (hl + lam) + gr * gr / (hr + lam) - parent_score
                    if gain > best[0]:
                        best = (gain, j, b)
            if best[1] < 0:
                return [-1, 0.0, None, None, value]
            _, j, b = best
            col = bins[j]
            left = [r for r in rows if col[r] <= b]
            right = [r for r in rows if col[r] > b]
            return [j, cuts[j][b], build(left, g, h, level + 1), build(right, g, h, level + 1), value]

        def apply(node, i):
            while node[0] >= 0:
                node = node[2] if cols[node[0]][i] <= node[1] else node[3]
            return node[4]

        for t in range(n_trees):
            p = [_sig(m) for m in margin]
            g = [(pi - yi) * wi for pi, yi, wi in zip(p, y, sw)]
            h = [max(pi * (1 - pi), 1e-6) * wi for pi, wi in zip(p, sw)]
            rows = [i for i in range(n) if rng.random() < subsample]
            tree = build(rows, g, h, 0)
            self.trees.append(tree)
            for i in range(n):
                margin[i] += apply(tree, i)
            if log and (t + 1) % 20 == 0:
                log(f"  tree {t + 1}/{n_trees}")

    def save(self, path: str = MODEL_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "gbdt", "features": self.features, "base": self.base,
                       "trees": self.trees, "meta": self.meta}, fh, separators=(",", ":"))


def load_model(path: str = MODEL_PATH):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if d.get("type") == "gbdt":
        return GBDTModel(d["features"], d["trees"], d["base"], d.get("meta"))
    return LogisticModel(d["features"], d["weights"], d["bias"], d["mean"], d["std"], d.get("meta"))


def auc(scores: list[float], labels: list[int]) -> float:
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        rank_sum += avg_rank * sum(l for _, l in pairs[i:j])
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def cascade_weight(dep) -> float:
    """Probability that a failing callee takes its caller down with it."""
    if not dep.hard:
        return 0.08
    return 0.9 * min(1.0, dep.calls / 0.35)


class Predictor:
    def __init__(self, topo: Topology, model=None):
        self.topo = topo
        if model is None:
            if not os.path.exists(MODEL_PATH):
                raise FileNotFoundError("No trained model found - run `python -m orca.train` first")
            model = load_model()
        self.model = model

    def predict(self, obs: Observer) -> dict[str, dict]:
        topo = self.topo
        out: dict[str, dict] = {}
        for n in topo.names:
            failed = obs.is_failed(n)
            f = obs.features(n)
            p = 1.0 if failed else self.model.predict(f)
            drivers = [] if failed else self.model.explain(f)
            # hybrid guard: monotone resource exhaustion is extrapolated deterministically
            res_eta = None if failed else obs.resource_eta(n)
            if res_eta is not None:
                hazard = 1.0 / (1.0 + math.exp((res_eta - HORIZON) / 3.0))
                if hazard > p:
                    p = hazard
                    drivers = [{"feature": "trend", "label": f"resource exhaustion in ~{res_eta:.0f} ticks (trend)",
                                "contribution": round(hazard * 4, 2), "value": round(res_eta, 1)}] + drivers[:3]
            out[n] = {"risk": p, "failed": failed, "eta": obs.trend_eta(n), "drivers": drivers}
        # noisy-OR propagation, callees first
        for n in reversed(topo.order):
            r = out[n]
            survive = 1.0 - r["risk"]
            via, via_w = None, 0.0
            for d in topo.callees[n]:
                # only risk above the background noise floor propagates
                excess = max(0.0, (out[d.callee]["cascade"] - RISK_FLOOR) / (1.0 - RISK_FLOOR))
                contrib = cascade_weight(d) * excess
                survive *= 1.0 - contrib
                if contrib > via_w:
                    via, via_w = d.callee, contrib
            r["cascade"] = 1.0 - survive
            r["via"] = via if via_w > 0.2 and via_w >= r["risk"] * 0.5 else None
            eta = r["eta"]
            if r["via"] is not None and not r["failed"]:
                dep_eta = out[r["via"]]["eta"]
                if dep_eta is not None:
                    hop = dep_eta + 2.0
                    eta = hop if eta is None else min(eta, hop)
            if eta is None and r["cascade"] >= ALERT and not r["failed"]:
                eta = float(HORIZON)
            r["eta"] = eta
        return out

    @staticmethod
    def summary(preds: dict[str, dict], gateway: str) -> dict:
        at_risk = sorted((n for n, p in preds.items() if not p["failed"] and p["cascade"] >= ALERT),
                         key=lambda n: -preds[n]["cascade"])
        return {
            "failed": [n for n, p in preds.items() if p["failed"]],
            "at_risk": at_risk,
            "expected_failures": round(sum(p["cascade"] for p in preds.values()), 2),
            "system_crash_risk": round(preds[gateway]["cascade"], 3),
            "system_crash_eta": preds[gateway]["eta"],
        }
