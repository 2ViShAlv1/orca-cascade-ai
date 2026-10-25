# ORCA - hackathon submission

**Problem statement:** 01 | Software & AI - predict cascading software failures, identify their root causes and
recommend the optimal recovery strategy before an entire system crashes.

| | |
|---|---|
| **Live deployment** | https://2vishalv1.github.io/orca-cascade-ai/ - opens straight into the live dashboard; the full engine runs in the visitor's browser, so there is no server to go down |
| **GitHub repository** | https://github.com/2ViShAlv1/orca-cascade-ai |
| **Demo video** (1 min 58 s, narrated) | https://2vishalv1.github.io/orca-cascade-ai/media/orca-demo.mp4 · also in the repo: [`docs/media/orca-demo.mp4`](docs/media/orca-demo.mp4) |
| **Team** | [Team name] - Vishal Verma [+ other members] |

## What it does (one paragraph)
ORCA watches a 17-microservice platform through metrics, raw log lines and deploy/crash events. It **predicts
which services will fail about 70 seconds before they do**, traces how the failure will spread through the
dependency graph, **names the root cause and fault type with evidence**, **simulates every recovery option on a
digital twin** and applies the best one (one click or autopilot with safety guardrails), then **writes the incident
post-mortem**. On 60 held-out chaos incidents: 92% of failures predicted in advance, 100% correct root cause and
fault type, 0 false alarms on fault-free runs, and user availability raised from 72.3% to 93.5%.

## How to try it in 60 seconds
1. Open the live link (first load downloads the Python runtime, a few seconds).
2. **Chaos lab → Product DB lock storm → Run scenario.** Watch the forecast flag services while they are still green.
3. Read the **ORCA briefing** and **Root cause analysis**, then click **Apply** on the top recovery strategy.
4. Click **Post-mortem** under *User availability*, and **Model card** in the header for the evaluation results.
5. **Reset → Autopilot → Leaky catalog release** to watch ORCA fix it before anything fails.

## Technologies used
| Layer | Technology |
|---|---|
| Language / runtime | **Python 3.10+, standard library only** (no third-party packages) |
| Simulation | Discrete-time microservice simulator / digital twin: queueing theory, Little's law, timeouts, retries, cache-miss amplification, OOM and liveness crash loops |
| Machine learning | **Gradient-boosted decision trees** implemented from scratch (histogram splits, logistic loss, Saabas path attribution for explanations); logistic-regression baseline |
| Graph algorithms | Noisy-OR cascade-risk propagation; **random walk with restart / personalised PageRank** for root-cause analysis |
| Log analytics | Regex log-template mining, intrinsic-vs-symptom classification, blame-pointer extraction |
| Anomaly detection | Adaptive EWMA baselines with robust z-scores, frozen during incidents |
| Decision making | What-if simulation of candidate strategies on a forked twin; risk-adjusted scoring; guarded autopilot policy |
| Backend | `http.server` JSON API + **Server-Sent Events** live stream |
| Frontend | Vanilla JavaScript, inline SVG charts and dependency graph, CSS custom properties (light/dark), no frameworks |
| Browser runtime | **Pyodide 0.26.4** (CPython compiled to WebAssembly) runs the same `orca` package in the browser for the static deployment |
| Hosting / CI | **GitHub Pages** + **GitHub Actions** (tests on Python 3.10 and 3.12, then automatic deploy); Dockerfile for container hosting |
| Testing | `unittest` - 20 unit and integration tests; held-out evaluation harness (`python -m orca.evaluate`) |
| Demo video | Recorded by driving the real dashboard over the Chrome DevTools Protocol; encoded with FFmpeg (H.264/AAC) |

## AI tools used
| Tool | How it was used |
|---|---|
| **ORCA's own models** (built here) | The failure predictor (gradient-boosted trees) is trained from scratch on 240 simulated incident histories (136,486 samples, `python -m orca.train`). RCA and planning are algorithmic. **No external AI API is called at runtime.** |
| **Claude Code (Anthropic), model Claude Opus 5.5** | AI coding assistant used to build the project: architecture and design, writing the code and tests, debugging, the evaluation and threshold sweep, the dashboard, documentation, the pitch deck and this video pipeline. |
| **Microsoft Speech API (Windows text-to-speech, voice "Zira")** | Narration of the demo video. |

## Results (held-out, reproducible)
| Metric | Value |
|---|---|
| Failures predicted before they happened | 92% (mean warning 14 ticks, about 70 s) |
| Precision of risk flags / false alarms on fault-free runs | 81% / 0% |
| Root cause: service (top-1) / fault type | 100% / 100% |
| User availability: no action → ORCA autopilot | 72.3% → 93.5% |
| Failure model AUC (vs logistic-regression baseline) | 0.889 (0.782) |

Full report: [`reports/evaluation.md`](reports/evaluation.md). Limitations are stated in the [README](README.md#limitations-and-next-steps).

## Run locally
```bash
git clone https://github.com/2ViShAlv1/orca-cascade-ai && cd orca-cascade-ai
python run.py          # http://127.0.0.1:8000  (or start.bat / ./start.sh / docker)
python -m unittest discover -s tests
python -m orca.evaluate --per-kind 10 --calm 6
```
