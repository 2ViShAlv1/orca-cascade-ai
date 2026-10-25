"""Recovery planning with what-if simulation on a digital twin.

The planner never peeks at the injected ground truth. It rebuilds the
*diagnosed* fault (root service + fault type from RCA, severity estimated from
observed trends) on a fork of the current operational state - replicas,
breakers, rate limits and in-flight actions - and plays every candidate
strategy forward. Strategies are ranked on projected user-facing
availability, how many services fail, time to recover, extra capacity cost
and the operational risk of the actions themselves.

If the diagnosis is wrong the projections are wrong too, which is exactly
how a human operator's mental model fails, so confidence is reported
alongside every recommendation.
"""
from __future__ import annotations

from .observer import Observer, slope
from .simulator import ACTION_CATALOG, FAILED_ERR, Fault, Simulator, action_targets

HORIZON = 60

RATIONALE = {
    "rollback": "removes the faulty release, eliminating the root cause",
    "restart": "clears the bad runtime state (heap, stuck queries, crashed process)",
    "scale_out": "adds capacity to absorb load at the bottleneck",
    "circuit_breaker": "stops callers piling onto the sick service so it can drain",
    "rate_limit": "sheds excess ingress to keep the core healthy",
    "failover": "moves traffic to a healthy standby without the contention",
    "purge_storage": "frees disk and fixes retention, eliminating the root cause",
    "purge_keys": "frees cache memory while keeping the hot set warm (no cold-cache stampede)",
}


CANONICAL_FIX = {
    "memory_leak": "rollback", "bad_deploy": "rollback", "db_slow_queries": "failover",
    "cache_crash": "purge_keys", "disk_fill": "purge_storage", "traffic_surge": "rate_limit",
}


def estimate_faults(obs: Observer, rca: dict) -> list[Fault]:
    """Rebuild every diagnosed fault (primary + strong secondary signatures) on the twin."""
    faults = _estimate_one(obs, rca["root"], rca["kind"])
    for sec in rca.get("secondary", []):
        if not any(f.target == sec["service"] for f in faults):
            faults += _estimate_one(obs, sec["service"], sec["kind"], primary=False)
    return faults


def _estimate_one(obs: Observer, root: str, kind: str, primary: bool = True) -> list[Fault]:
    """One fault, calibrated to observed trends."""
    m = obs.latest(root)
    t = obs.tick
    if kind in ("memory_leak", "cache_crash"):
        rate = slope(obs.up_series(root, "mem", 12))
        per_sev = 0.011 if kind == "memory_leak" else 0.012
        sev = max(0.3, min(2.0, rate / per_sev)) if rate > 0.001 else 1.0
        return [Fault(kind, root, t, sev, 0)]
    if kind == "db_slow_queries":
        base = obs.baseline_value(root, "lat") or m["lat"]
        ratio = max(1.0, m["lat"] / max(base, 0.5))
        ramp = min(1.0, (ratio - 1.0) / 9.0)
        return [Fault(kind, root, t, 1.0, int(ramp * 45))]
    if kind == "disk_fill":
        rate = slope(obs.up_series(root, "disk", 12))
        sev = max(0.3, min(2.0, rate / 0.007)) if rate > 0.0005 else 1.0
        return [Fault(kind, root, t, sev, 0)]
    if kind == "bad_deploy":
        dep_err = 0.0
        own = m["err"]
        sev = max(0.3, min(2.0, (own - dep_err) / 0.4))
        return [Fault(kind, root, t, sev, 15)]
    if kind == "traffic_surge":
        base = obs.baseline_value(root, "rps") or m["rps"]
        ratio = max(1.0, m["rps"] / max(base, 0.1))
        growth = max(0.0, slope([v / base for v in obs.series(root, "rps", 8)]))
        # extrapolate the ramp ~25 ticks ahead (capped), then assume it persists
        peak = min(3.5, max(ratio, ratio + growth * 25))
        sev = max(0.05, (peak - 1.0) / 1.2)
        progress = int(40 * (ratio - 1.0) / max(peak - 1.0, 1e-6)) if peak > ratio else 40
        return [Fault(kind, root, t, sev, min(40, progress))]
    if not primary:
        return []
    # unclassified degradation: if load is not elevated, assume the excess latency persists;
    # load-driven overload needs no synthetic fault (the elevated load is already in the twin)
    base = obs.baseline_value(root, "lat") or m["lat"]
    ratio = max(1.0, m["lat"] / max(base, 0.5))
    rps_ratio = m["rps"] / max(obs.baseline_value(root, "rps") or m["rps"], 0.1)
    if ratio > 1.5 and not m["down"] and rps_ratio < 1.3:
        return [Fault("db_slow_queries", root, t, 1.0, int(min(1.0, (ratio - 1.0) / 9.0) * 45))]
    return []


def candidate_strategies(obs: Observer, rca: dict, preds: dict, topo) -> list[list[tuple[str, str]]]:
    root, kind = rca["root"], rca["kind"]
    gw = next(n for n in topo.names if topo.services[n].kind == "edge")
    ok = lambda a, s: s in action_targets(topo, a)  # noqa: E731
    bottleneck = rca.get("bottleneck")
    at_risk = sorted((n for n in topo.names if topo.services[n].scalable and topo.services[n].kind != "edge"),
                     key=lambda n: -preds[n]["cascade"])
    hot = at_risk[0] if at_risk else None
    rm = obs.latest(root)
    root_base_lat = obs.baseline_value(root, "lat") or rm["lat"]
    # a breaker on a healthy service only fails requests; offer it once the service is sick
    sick = rm["down"] or rm["err"] >= 0.1 or rm["lat"] / max(root_base_lat, 0.5) >= 3.0
    c: list[list[tuple[str, str]]] = [[]]
    if kind in ("memory_leak", "bad_deploy"):
        c += [[("rollback", root)], [("restart", root)], [("rollback", root), ("scale_out", root)]]
        if ok("circuit_breaker", root) and sick:
            c.append([("circuit_breaker", root), ("rollback", root)])
    elif kind == "cache_crash":
        if not rm["down"]:
            c += [[("purge_keys", root)], [("purge_keys", root), ("rate_limit", gw)]]
        c += [[("restart", root)], [("restart", root), ("rate_limit", gw)], [("rate_limit", gw)]]
    elif kind == "db_slow_queries":
        c += [[("failover", root)], [("restart", root)], [("failover", root), ("rate_limit", gw)]]
        if sick:
            c.append([("circuit_breaker", root)])
    elif kind == "disk_fill":
        c += [[("purge_storage", root)], [("restart", root)]]
    elif kind == "traffic_surge":
        b = bottleneck if bottleneck and ok("scale_out", bottleneck) else hot
        base_rps = topo.services[gw].ingress
        cap = lambda x: ("rate_limit", gw, {"max_rps": round(base_rps * x)})  # noqa: E731
        hot3 = [("scale_out", n) for n in ([b] if b else []) + [n for n in at_risk if n != b][:2]]
        c += [[cap(1.2)], [cap(1.5)], hot3, [cap(1.5)] + hot3, [cap(1.7)] + hot3]
        if ok("circuit_breaker", "recommendation-service"):
            c.append([("circuit_breaker", "recommendation-service"), cap(1.5)] + hot3)
    else:
        target = bottleneck or root
        if ok("scale_out", target):
            c.append([("scale_out", target)])
        c += [[("restart", root)], [("rate_limit", gw)]]
    # fixes for independent secondary faults, alone and combined with the primary fix
    primary_fix = CANONICAL_FIX.get(kind)
    for sec in rca.get("secondary", [])[:2]:
        fix = CANONICAL_FIX.get(sec["kind"])
        tgt = gw if fix == "rate_limit" else sec["service"]
        if not fix or not ok(fix, tgt) or (fix == "purge_keys" and obs.latest(tgt)["down"]):
            continue
        sec_action = ("rate_limit", gw, {"max_rps": round(topo.services[gw].ingress * 1.5)}) if fix == "rate_limit" \
            else (fix, tgt)
        c.append([sec_action])
        p_tgt = gw if primary_fix == "rate_limit" else root
        if primary_fix and ok(primary_fix, p_tgt) and not (primary_fix == "purge_keys" and rm["down"]):
            p_action = ("rate_limit", gw, {"max_rps": round(topo.services[gw].ingress * 1.5)}) \
                if primary_fix == "rate_limit" else (primary_fix, p_tgt)
            c.append([p_action, sec_action])
    # generic alternatives an operator might reach for
    if ok("restart", root) and [("restart", root)] not in c:
        c.append([("restart", root)])
    if hot and ok("scale_out", hot) and [("scale_out", hot)] not in c:
        c.append([("scale_out", hot)])
    seen, out = set(), []
    for s in c:
        s = [a if len(a) == 3 else (a[0], a[1], {}) for a in s]
        key = tuple((k, t, tuple(sorted(p.items()))) for k, t, p in s)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out[:12]


def simulate(twin: Simulator, actions: list[tuple[str, str, dict]], horizon: int = HORIZON) -> dict:
    sim = twin.fork(noise=False)
    base_pods = sum(st.replicas + st.pending for st in sim.state.values())
    for kind, target, params in actions:
        sim.apply_action(kind, target, **params)
    names = sim.topo.names
    series, failed_counts = [], []
    for _ in range(horizon):
        sim.step()
        series.append(sim.availability)
        failed_counts.append(sum(1 for n in names if sim.state[n].down or sim.state[n].err >= FAILED_ERR))
    extra = sum(st.replicas + st.pending for st in sim.state.values()) - base_pods
    avg = sum(series) / horizon
    final = sum(series[-15:]) / 15
    recover = None
    for i in range(horizon):
        if all(v >= 0.95 for v in series[i:]):
            recover = i
            break
    risk = sum(ACTION_CATALOG[k]["risk"] for k, _, _ in actions)
    return {
        "series": [round(v, 4) for v in series],
        "availability": avg,
        "final": final,
        "peak_failed": max(failed_counts),
        "avg_failed": sum(failed_counts) / horizon,
        "recover_ticks": recover,
        "risk": risk,
        "extra_pods": max(0, extra),
    }


def score(r: dict) -> float:
    return (100.0 * (0.6 * r["availability"] + 0.4 * r["final"]) - 1.5 * r["avg_failed"]
            - 0.4 * r["extra_pods"] - 3.0 * r["risk"])


REPEAT_COOLDOWN = 60


def should_act(plan: dict | None, diagnosis: dict | None, summary: dict, alert: float,
               history: list[dict] | None = None, tick: int = 0) -> tuple[dict | None, str | None]:
    """Autopilot policy: (strategy to apply now or None, escalation reason or None).

    Act when something is about to fail and the plan beats waiting, or earlier
    when the diagnosis is confident and the projected win is large - fixing a
    leak at 70% heap is safer than during the rollout at 98%.
    Guardrail: never repeat an action on the same target within REPEAT_COOLDOWN
    ticks - if it did not work, the model of the incident is wrong and a human
    should look (escalate) rather than the autopilot looping.
    """
    if not plan or not diagnosis:
        return None, None
    base = next(s for s in plan["strategies"] if s["id"] == plan["baseline"])
    recent = {(h["kind"], h["target"]) for h in (history or []) if tick - h["tick"] < REPEAT_COOLDOWN}
    urgent = bool(summary.get("failed")) or bool(summary.get("at_risk"))
    conf = diagnosis["confidence"]
    skipped = None
    for s in plan["strategies"]:
        if not s["actions"]:
            break  # waiting beats everything ranked below
        if any((a["kind"], a["target"]) in recent for a in s["actions"]):
            skipped = skipped or s
            continue
        gain = s["score"] - base["score"]
        if (urgent and conf >= 0.4 and gain >= 1.5) or (conf >= 0.7 and gain >= 5.0):
            return s, None
        break
    if skipped and urgent:
        return None, (f"'{skipped['label']}' was already applied in the last {REPEAT_COOLDOWN} ticks "
                      "without resolving the incident - escalating to on-call instead of repeating it")
    return None, None


def describe(actions: list[tuple[str, str, dict]]) -> str:
    if not actions:
        return "No action (observe)"
    parts = []
    for k, t, p in actions:
        if k == "rate_limit" and p.get("max_rps"):
            parts.append(f"Rate-limit ingress to {p['max_rps']:.0f} req/s")
        else:
            parts.append(f"{ACTION_CATALOG[k]['label']} {t}")
    return " + ".join(parts)


class RecoveryPlanner:
    def __init__(self, topo, horizon: int = HORIZON):
        self.topo = topo
        self.horizon = horizon

    def plan(self, sim: Simulator, obs: Observer, rca: dict, preds: dict) -> dict:
        faults = estimate_faults(obs, rca)
        twin = sim.fork(faults=faults, noise=False)
        results = []
        for actions in candidate_strategies(obs, rca, preds, self.topo):
            r = simulate(twin, actions, self.horizon)
            r["actions"] = [{"kind": k, "target": t, "params": p} for k, t, p in actions]
            r["label"] = describe(actions)
            r["score"] = score(r)
            results.append(r)
        baseline = next(r for r in results if not r["actions"])
        results.sort(key=lambda r: -r["score"])
        for i, r in enumerate(results):
            r["id"] = i
            r["rank"] = i + 1
            r["delta"] = r["availability"] - baseline["availability"]
            r["rationale"] = self._rationale(r, baseline)
            for key in ("availability", "final", "avg_failed", "risk", "score", "delta"):
                r[key] = round(r[key], 4)
        best = results[0]
        if not best["actions"]:
            headline = ("Projected to self-heal: no intervention beats waiting." if rca["confidence"] >= 0.5 else
                        f"Diagnosis still forming ({rca['confidence'] * 100:.0f}% confidence): "
                        "no intervention is projected to help yet.")
        else:
            headline = (f"{best['label']}: projected availability {best['availability'] * 100:.1f}% "
                        f"vs {baseline['availability'] * 100:.1f}% with no action.")
        return {
            "tick": obs.tick,
            "horizon": self.horizon,
            "hypothesis": {"root": rca["root"], "kind": rca["kind"], "confidence": rca["confidence"],
                           "modelled_faults": [{"kind": f.kind, "target": f.target,
                                                "severity": round(f.severity, 2)} for f in faults]},
            "strategies": results,
            "best": best["id"],
            "baseline": baseline["id"],
            "headline": headline,
        }

    def _rationale(self, r: dict, baseline: dict) -> str:
        if not r["actions"]:
            if baseline["peak_failed"] == 0:
                return "Twin projects the system stays within SLO without intervention."
            rec = ("never recovers" if r["recover_ticks"] is None
                   else f"recovers after {r['recover_ticks']} ticks")
            return f"Twin projects up to {r['peak_failed']} services failing; {rec} within the horizon."
        parts = [RATIONALE[a["kind"]] for a in r["actions"]]
        why = "; ".join(dict.fromkeys(parts))
        outcome = (f"recovers to >=95% in {r['recover_ticks']} ticks" if r["recover_ticks"] is not None
                   else "does not fully recover within the horizon")
        return f"{why[0].upper()}{why[1:]}. Projected: {outcome}, peak {r['peak_failed']} failed services."
