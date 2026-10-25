"""Offline evaluation on held-out chaos episodes (seeds disjoint from training).

    python -m orca.evaluate [--per-kind 8] [--calm 6]

Measures, per fault type:
* prediction - did ORCA flag services before they failed, how early, and how
  many flags were false alarms; plus false alarms on fault-free runs,
* root cause - top-1 / top-3 service accuracy and fault-type accuracy,
* recovery  - ORCA's recommended strategy is applied to a fork of the *real*
  (ground-truth) system and compared with doing nothing and with the textbook
  expert fix, on user-facing availability over the next 60 ticks.
Writes a markdown report to reports/evaluation.md.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import defaultdict

from .engine import Pipeline
from .predictor import ALERT
from .recovery import HORIZON as PLAN_HORIZON
from .recovery import should_act
from .scenarios import EXPERT_FIX
from .simulator import FAULT_CATALOG, fault_targets
from .topology import Topology

REPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "evaluation.md")
FLAG = ALERT
SLACK = 10  # a flag counts as correct if the service fails within HORIZON + SLACK ticks


def rollout(pipe: Pipeline, actions: list[dict], ticks: int = PLAN_HORIZON) -> float:
    sim = pipe.sim.fork(noise=False)  # keeps the true faults
    for a in actions:
        try:
            sim.apply_action(a["kind"], a["target"], **(a.get("params") or {}))
        except ValueError:  # e.g. flushing keys on a cache that already crashed
            sim.apply_action("restart", a["target"])
    total = 0.0
    for _ in range(ticks):
        sim.step()
        total += sim.availability
    return total / ticks


def closed_loop(seed: int, kind: str, target: str, sev: float, topo: Topology, autopilot: bool,
                ticks: int = 80) -> float:
    """Mean availability over `ticks` after injection, with or without ORCA's autopilot policy."""
    pipe = Pipeline(topo, seed=seed)
    pipe.sim.inject(kind, target, sev)
    last_plan, last_act, plan = -99, -99, None
    total = 0.0
    for _ in range(ticks):
        pipe.step()
        total += pipe.sim.availability
        t = pipe.sim.tick
        if not autopilot or not pipe.diagnosis:
            continue
        if t - last_plan >= 3:
            plan, last_plan = pipe.plan(), t
        if plan and t - last_act >= 20 and not pipe.sim.scheduled:
            history = [{"tick": e["tick"], "kind": e["action"], "target": e["service"]}
                       for e in pipe.sim.events if e["type"] == "action"]
            best, _ = should_act(plan, pipe.diagnosis, pipe.summary, ALERT, history, t)
            if best:
                for a in best["actions"]:
                    pipe.sim.apply_action(a["kind"], a["target"], **(a.get("params") or {}))
                last_act, last_plan = t, -99
    return total / ticks


def run_fault_episode(seed: int, kind: str, topo: Topology) -> dict:
    rng = random.Random(seed)
    target = rng.choice(fault_targets(topo, kind))
    sev = round(rng.uniform(0.8, 1.3), 2)
    pipe = Pipeline(topo, seed=seed)
    pipe.sim.inject(kind, target, sev)
    t0 = pipe.sim.tick
    truth_root = target
    first_flag: dict[str, int] = {}
    first_fail: dict[str, int] = {}
    diag = None
    rec = None
    for i in range(110):
        pipe.step()
        t = pipe.sim.tick
        for n, p in pipe.preds.items():
            if p["failed"]:
                first_fail.setdefault(n, t)
            elif p["cascade"] >= FLAG and n not in first_fail:
                first_flag.setdefault(n, t)
        # evaluate RCA + recovery at the moment ORCA would first recommend an action
        if diag is None and pipe.diagnosis and (pipe.summary["at_risk"] or pipe.summary["failed"]):
            diag = dict(pipe.diagnosis, at=t)
            plan = pipe.plan()
            best = plan["strategies"][0]
            expert_kind = EXPERT_FIX[kind]
            expert = [{"kind": expert_kind, "target": None if expert_kind == "rate_limit" else target}]
            if expert_kind == "rate_limit":
                expert[0]["target"] = "api-gateway"
            rec = {
                "strategy": best["label"],
                "orca": rollout(pipe, best["actions"]),
                "none": rollout(pipe, []),
                "expert": rollout(pipe, expert),
            }
    # prediction scoring
    tp_lead = []
    missed = []
    for n, tf in first_fail.items():
        tfl = first_flag.get(n)
        if tfl is not None and tfl < tf:
            tp_lead.append(tf - tfl)
        else:
            missed.append(n)
    false_flags = [n for n, tfl in first_flag.items()
                   if n not in first_fail or first_fail[n] - tfl > 15 + SLACK]
    sys_fail = min(first_fail.values()) if first_fail else None
    sys_flag = min(first_flag.values()) if first_flag else None
    out = {
        "kind": kind, "target": target, "severity": sev, "inject": t0,
        "failed": len(first_fail), "caught": len(tp_lead), "missed": len(missed), "lead": tp_lead,
        "flags": len(first_flag), "false_flags": len(false_flags),
        "system_lead": (sys_fail - sys_flag) if sys_fail and sys_flag and sys_flag < sys_fail else (0 if sys_fail else None),
        "diag": diag, "rec": rec,
        "loop_none": closed_loop(seed, kind, target, sev, topo, autopilot=False),
        "loop_orca": closed_loop(seed, kind, target, sev, topo, autopilot=True),
    }
    if diag:
        ranked = [r["service"] for r in diag["ranking"]]
        out["rca_top1"] = diag["root"] == truth_root
        out["rca_top3"] = truth_root in ranked[:3]
        out["rca_kind"] = diag["kind"] == kind
        out["rca_at"] = diag["at"] - t0
    return out


def run_calm_episode(seed: int, topo: Topology, ticks: int = 200) -> int:
    pipe = Pipeline(topo, seed=seed)
    flagged_ticks = 0
    for _ in range(ticks):
        pipe.step()
        if pipe.summary["at_risk"] or pipe.summary["failed"]:
            flagged_ticks += 1
    return flagged_ticks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-kind", type=int, default=8)
    ap.add_argument("--calm", type=int, default=6)
    ap.add_argument("--seed", type=int, default=50000)
    args = ap.parse_args()
    topo = Topology()
    t_start = time.time()
    results = defaultdict(list)
    for k_i, kind in enumerate(FAULT_CATALOG):
        for e in range(args.per_kind):
            r = run_fault_episode(args.seed + k_i * 1000 + e, kind, topo)
            results[kind].append(r)
        print(f"  {kind}: {args.per_kind} episodes done ({time.time() - t_start:.0f}s)")
    calm = [run_calm_episode(args.seed + 90000 + i, topo) for i in range(args.calm)]

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else float("nan")

    rows = []
    agg = defaultdict(list)
    for kind, rs in results.items():
        failed = sum(r["failed"] for r in rs)
        caught = sum(r["caught"] for r in rs)
        flags = sum(r["flags"] for r in rs)
        false_flags = sum(r["false_flags"] for r in rs)
        leads = [x for r in rs for x in r["lead"]]
        diag = [r for r in rs if r["diag"]]
        recs = [r["rec"] for r in rs if r["rec"]]
        row = {
            "kind": kind,
            "episodes": len(rs),
            "recall": caught / failed if failed else float("nan"),
            "precision": (flags - false_flags) / flags if flags else float("nan"),
            "lead": mean(leads),
            "sys_lead": mean([r["system_lead"] for r in rs]),
            "top1": mean([float(r["rca_top1"]) for r in diag]),
            "top3": mean([float(r["rca_top3"]) for r in diag]),
            "kind_acc": mean([float(r["rca_kind"]) for r in diag]),
            "rca_at": mean([r["rca_at"] for r in diag]),
            "none": mean([x["none"] for x in recs]),
            "orca": mean([x["orca"] for x in recs]),
            "expert": mean([x["expert"] for x in recs]),
            "loop_none": mean([r["loop_none"] for r in rs]),
            "loop_orca": mean([r["loop_orca"] for r in rs]),
        }
        rows.append(row)
        for key in ("recall", "precision", "lead", "sys_lead", "top1", "top3", "kind_acc", "none", "orca", "expert",
                    "loop_none", "loop_orca"):
            agg[key].append(row[key])
        for k2 in ("failed", "caught", "flags", "false_flags"):
            agg["_" + k2].append(sum(r[k2] for r in rs))

    overall = {k: mean(v) for k, v in agg.items() if not k.startswith("_")}
    overall["recall"] = sum(agg["_caught"]) / max(sum(agg["_failed"]), 1)
    overall["precision"] = (sum(agg["_flags"]) - sum(agg["_false_flags"])) / max(sum(agg["_flags"]), 1)
    calm_rate = sum(calm) / (len(calm) * 200) if calm else float("nan")

    from .predictor import load_model
    meta = load_model().meta
    lines = [
        "# ORCA evaluation report",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M')} on {sum(len(v) for v in results.values())} held-out chaos "
        f"episodes (seeds {args.seed}+, disjoint from training) and {len(calm)} fault-free runs "
        f"in {time.time() - t_start:.0f}s.",
        "",
        f"Failure model: {meta.get('model', 'model')} - validation AUC **{meta.get('val_auc', float('nan')):.3f}** "
        f"(logistic-regression baseline {meta.get('baseline_logreg_val_auc', float('nan')):.3f}), "
        f"{meta.get('samples', '?')} training samples, horizon {meta.get('horizon', '?')} ticks (1 tick = 5 s).",
        "",
        "## Headline",
        "",
        f"* **{overall['recall'] * 100:.0f}%** of service failures were predicted before they happened, "
        f"on average **{overall['lead']:.1f} ticks (~{overall['lead'] * 5:.0f} s) early**; "
        f"the first failure of each incident was anticipated **{overall['sys_lead']:.1f} ticks** ahead.",
        f"* **{overall['precision'] * 100:.0f}%** of risk flags were followed by a real failure "
        f"(fault-free runs: {calm_rate * 100:.1f}% of ticks raised any flag).",
        f"* Root cause: top-1 service **{overall['top1'] * 100:.0f}%**, top-3 **{overall['top3'] * 100:.0f}%**, "
        f"fault type **{overall['kind_acc'] * 100:.0f}%** - at the moment the first recommendation is made.",
        f"* Recovery (60-tick user availability on the real system): no action **{overall['none'] * 100:.1f}%** -> "
        f"ORCA's first recommendation **{overall['orca'] * 100:.1f}%** (textbook expert fix {overall['expert'] * 100:.1f}%).",
        f"* Closed loop (ORCA autopilot re-planning every 3 ticks, 80 ticks from fault injection): "
        f"availability **{overall['loop_none'] * 100:.1f}% -> {overall['loop_orca'] * 100:.1f}%**.",
        "",
        "## Per fault type",
        "",
        "| Fault | Episodes | Failures predicted | Flag precision | Mean lead (ticks) | Incident lead | RCA top-1 | RCA top-3 | Fault type | Diagnosed after | Avail. no action | Avail. ORCA 1st rec. | Avail. expert | Autopilot: none -> ORCA |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def f(v, pctg=False, d=1):
        if v != v:  # nan
            return "n/a"
        return f"{v * 100:.0f}%" if pctg else f"{v:.{d}f}"

    for r in rows:
        lines.append(
            f"| {FAULT_CATALOG[r['kind']]['label']} | {r['episodes']} | {f(r['recall'], True)} | {f(r['precision'], True)} | "
            f"{f(r['lead'])} | {f(r['sys_lead'])} | {f(r['top1'], True)} | {f(r['top3'], True)} | {f(r['kind_acc'], True)} | "
            f"{f(r['rca_at'])} ticks | {f(r['none'], True)} | {f(r['orca'], True)} | {f(r['expert'], True)} | "
            f"{f(r['loop_none'], True)} -> {f(r['loop_orca'], True)} |")
    lines += ["", "## Recommendations made", "",
              "| Fault | Target | Diagnosis | Recommended strategy | No action | ORCA |", "|---|---|---|---|---|---|"]
    for kind, rs in results.items():
        for r in rs:
            if r["rec"]:
                d = r["diag"]
                lines.append(f"| {kind} | {r['target']} | {d['kind']} @ {d['root']} ({d['confidence'] * 100:.0f}%) | "
                             f"{r['rec']['strategy']} | {r['rec']['none'] * 100:.1f}% | {r['rec']['orca'] * 100:.1f}% |")
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    clean = lambda v: None if isinstance(v, float) and v != v else v  # noqa: E731  (NaN -> null)
    with open(REPORT.replace(".md", ".json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M"),
                   "episodes": sum(len(v) for v in results.values()), "calm_runs": len(calm),
                   "calm_flag_rate": clean(calm_rate), "model": meta,
                   "overall": {k: clean(v) for k, v in overall.items()},
                   "per_kind": [{**{k: clean(v) for k, v in r.items()}, "label": FAULT_CATALOG[r["kind"]]["label"]}
                                for r in rows]}, fh, indent=1)
    print("\n".join(lines[:22]))
    print(f"\nfull report: {REPORT}")


if __name__ == "__main__":
    main()
