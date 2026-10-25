# ORCA - pitch, demo and Q&A notes

## 30-second pitch
Modern systems don't crash all at once - one slow database exhausts the connection pools of everything that calls
it, retries pile on, and three minutes later the whole site is down. On-call engineers see the *symptoms* first
and spend those minutes searching for the cause. **ORCA predicts which services will fail about 70 seconds before
they do, names the root cause with evidence (100% accurate on our held-out tests), simulates every recovery option
on a digital twin and applies the best one - lifting availability from 72% to 93.5% across six fault types.**

## 5-minute structure
| Time | Beat | Show |
|---|---|---|
| 0:00 | The problem: cascades, MTTR, alert storms | Dependency map, all green |
| 0:40 | Inject *Product DB lock storm* | Forecast flags 5 services with countdowns; briefing names product-db |
| 1:30 | Why it believes that | RCA evidence: slow-query logs, 20 timeout lines blaming product-db, onset order; click a node for "why it may fail" |
| 2:10 | Compare strategies | Projection curves: failover ~88% vs ~35% no action; Apply; map turns green |
| 2:50 | Post-mortem in one click | Lead time per service, evidence, actions, prevention |
| 3:20 | Autopilot | Reset → Autopilot → Leaky catalog release: rollback at 60% heap, zero failures |
| 4:00 | Proof | Model card: 92% predicted ~70 s early, RCA 100%, 0 false alarms, 72% → 93.5% |
| 4:30 | Architecture + what's next | Pipeline diagram; Prometheus/OpenTelemetry/K8s integration |

## Talking points
* **Not scripted**: cascades emerge from queueing, Little's law, timeouts, retries and cache misses. ORCA sees only
  metrics, raw logs and change events - never the injected fault.
* **Explainable at every step**: tree-path attributions for each prediction, evidence list for the root cause,
  projected curves for each strategy, a written post-mortem.
* **Hybrid AI**: ML where patterns are fuzzy (GBDT risk), physics where trends are exact (time-to-exhaustion),
  graph algorithms where structure matters (noisy-OR cascade, random-walk RCA), simulation where decisions are
  irreversible (digital-twin what-ifs).
* **Safe autonomy**: confidence thresholds, no breakers on healthy services, never repeats a failed remediation -
  escalates to a human instead.
* **Runs anywhere**: zero dependencies, one command, 20 tests, reproducible evaluation.

## Likely questions
**Isn't it easy on simulated data?** The analyser never sees ground truth, failures emerge from system physics
rather than labels, and evaluation uses held-out seeds with random targets and severities. We report the weak
spots: memory-leak and bad-deploy flags have ~47% precision because many degraded services stop just short of the
failure threshold; the compound surge-plus-DB case can end in escalation.

**How would this work in production?** Swap the simulator for Prometheus/OpenTelemetry metrics, a log pipeline
and the Kubernetes event stream; take the topology from distributed traces; fit the twin's capacity and latency
curves from traces; fine-tune the predictor on incident history. The observer, predictor, RCA and planner code
paths stay the same.

**Why gradient-boosted trees rather than deep learning?** Tabular features, a need for exact explanations and
tiny inference cost: GBDT reaches AUC 0.889 vs 0.782 for logistic regression, trains in about 4 minutes in pure
Python and explains every prediction exactly.

**How do you pick the "optimal" strategy?** Each candidate is simulated 60 ticks forward on a fork of the live
state under ORCA's diagnosis and scored on mean and final availability, failed services, extra capacity and action
risk. On held-out incidents ORCA's choice matches or beats the textbook fix (92.4% vs 91.9% availability).

**What if the diagnosis is wrong?** The projections are then wrong too, so confidence is always shown, autopilot
needs high confidence or imminent failure, and repeated ineffective actions trigger escalation, not loops.

**Why the 0.6 alert threshold?** A sweep: 0.5 gives 95% recall / 72% precision, 0.6 gives 93% / 79% with ~70 s
warning, 0.7 gives 86% / 86% with less warning time.
