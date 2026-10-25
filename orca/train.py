"""Train the failure predictor on simulated incident histories.

    python -m orca.train [--episodes 180] [--seed 1000]

Each episode warms the estate up, injects random chaos (sometimes two
overlapping faults, sometimes a remediation half-way through) and records,
for every service and tick, the observer's feature vector together with the
label "this service crosses the failure threshold within HORIZON ticks".
Episodes are split 80/20 for a held-out validation AUC.
"""
from __future__ import annotations

import argparse
import random
import time

from .observer import Observer
from .predictor import HORIZON, MODEL_PATH, GBDTModel, LogisticModel, auc
from .scenarios import EXPERT_FIX, random_episode
from .simulator import Simulator
from .topology import Topology

CALM_KEEP = 0.12  # fraction of uneventful negative samples kept


def run_episode(seed: int, topo: Topology, ticks: int = 150) -> tuple[list[dict], list[int], list[float]]:
    rng = random.Random(seed)
    sim = Simulator(topo, seed=seed)
    obs = Observer(topo)
    plan = random_episode(rng, topo)
    fix_at = None
    if plan and rng.random() < 0.35:
        fix_at = plan[0][0] + rng.randint(10, 45)
    feats: list[list[dict | None]] = []
    failed: list[dict[str, bool]] = []
    for _ in range(ticks):
        for (t, kind, target, sev) in plan:
            if sim.tick == t:
                sim.inject(kind, target, sev)
        if fix_at is not None and sim.tick == fix_at:
            kind, target = plan[0][1], plan[0][2]
            action = EXPERT_FIX[kind] if rng.random() < 0.7 else rng.choice(["restart", "scale_out"])
            try:
                sim.apply_action(action, None if action == "rate_limit" else target)
            except ValueError:
                pass
        logs = sim.step()
        obs.ingest(sim.tick, sim.observe(), logs, sim.events)
        row = {}
        fl = {}
        for n in topo.names:
            fl[n] = obs.is_failed(n)
            row[n] = None if fl[n] or sim.tick <= 25 else (obs.features(n), obs.score[n])
        feats.append(row)
        failed.append(fl)
    X, y, w = [], [], []
    for i in range(len(feats) - HORIZON):
        for n in topo.names:
            item = feats[i][n]
            if item is None:
                continue
            f, score = item
            label = int(any(failed[j][n] for j in range(i + 1, i + 1 + HORIZON)))
            interesting = label or score > 2.0 or f["dep_anomaly"] > 0.2
            if not interesting:
                if rng.random() > CALM_KEEP:
                    continue
            X.append(f)
            y.append(label)
            w.append(1.0 if interesting else 1.0 / CALM_KEEP)  # undo subsampling => calibrated risk
    return X, y, w


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=180)
    ap.add_argument("--seed", type=int, default=1000)
    args = ap.parse_args()
    topo = Topology()
    t0 = time.time()
    train_X, train_y, train_w, val_X, val_y = [], [], [], [], []
    for e in range(args.episodes):
        X, y, w = run_episode(args.seed + e, topo)
        if e % 5 == 4:
            val_X += X
            val_y += y
        else:
            train_X += X
            train_y += y
            train_w += w
        if (e + 1) % 20 == 0:
            print(f"  simulated {e + 1}/{args.episodes} episodes ({time.time() - t0:.0f}s)")
    print(f"train samples {len(train_X)} (positives {sum(train_y)}), validation {len(val_X)} (positives {sum(val_y)})")
    baseline = LogisticModel()
    baseline.fit(train_X, train_y, weights=train_w)
    lr_auc = auc([baseline.predict(f) for f in val_X], val_y)
    print(f"logistic regression baseline: validation AUC {lr_auc:.3f}")

    model = GBDTModel()
    model.fit(train_X, train_y, weights=train_w, log=print)
    tr_auc = auc([model.predict(f) for f in train_X], train_y)
    va_scores = [model.predict(f) for f in val_X]
    va_auc = auc(va_scores, val_y)
    tp = sum(1 for s, l in zip(va_scores, val_y) if s >= 0.5 and l)
    fp = sum(1 for s, l in zip(va_scores, val_y) if s >= 0.5 and not l)
    fn = sum(1 for s, l in zip(va_scores, val_y) if s < 0.5 and l)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    model.meta = {"model": "gradient-boosted trees", "trees": len(model.trees),
                  "train_auc": round(tr_auc, 4), "val_auc": round(va_auc, 4),
                  "baseline_logreg_val_auc": round(lr_auc, 4),
                  "val_precision@0.5": round(precision, 4), "val_recall@0.5": round(recall, 4),
                  "samples": len(train_X), "episodes": args.episodes, "horizon": HORIZON,
                  "trained_at": time.strftime("%Y-%m-%d %H:%M")}
    model.save()
    print(f"GBDT: train AUC {tr_auc:.3f} | validation AUC {va_auc:.3f} precision {precision:.3f} recall {recall:.3f}")
    imp = {name: 0.0 for name in model.features}
    for f, _ in zip(val_X[:3000], range(3000)):
        _, contrib = model._margin_and_contrib(f)
        for name, c in zip(model.features, contrib):
            imp[name] += abs(c)
    print("mean |contribution| on validation:")
    for name, v in sorted(imp.items(), key=lambda x: -x[1])[:12]:
        print(f"  {name:16s} {v / min(len(val_X), 3000):.3f}")
    print(f"saved {MODEL_PATH} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
