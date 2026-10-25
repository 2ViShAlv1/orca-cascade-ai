"""Natural-language briefings and automatic incident post-mortems."""
from __future__ import annotations

import time

from .predictor import ALERT
from .rca import FAULT_LABELS

PREVENTION = {
    "memory_leak": [
        "Add a heap-growth soak test to the release pipeline (fail the build if heap grows >2%/min under steady load).",
        "Alert on memory slope, not just level - ORCA's trend hazard flagged this long before the OOM kill.",
        "Use canary releases with automatic rollback on memory regression.",
    ],
    "bad_deploy": [
        "Gate rollouts on canary error rate (automatic rollback above 2% errors).",
        "Add contract tests for the failing code path to CI.",
        "Keep the previous release warm so rollback takes seconds, not minutes.",
    ],
    "traffic_surge": [
        "Keep an adaptive ingress rate limiter armed at ~1.5x normal peak.",
        "Pre-scale stateless tiers ahead of planned events and add read replicas for hot databases.",
        "Degrade non-critical features (recommendations) automatically under load.",
    ],
    "db_slow_queries": [
        "Set statement and lock timeouts so a single long transaction cannot hold row locks indefinitely.",
        "Alert on lock-wait time and slow-query rate per table.",
        "Give callers bounded connection pools and circuit breakers so DB slowness cannot exhaust their workers.",
    ],
    "cache_crash": [
        "Cap Redis memory with an eviction policy and alert at 75% of maxmemory.",
        "Find and split oversized keys; add key-size limits in the client library.",
        "Protect databases from cache-miss stampedes with request coalescing and a DB-side rate limit.",
    ],
    "disk_fill": [
        "Enforce retention policies and alert on disk growth rate (time-to-full < 24h).",
        "Auto-expand volumes or offload old segments to object storage.",
    ],
    "overload": [
        "Right-size capacity for the observed peak and enable horizontal autoscaling on saturation.",
        "Add load-shedding at the edge for overload protection.",
    ],
}


def _eta_txt(ticks, tick_seconds: float) -> str:
    if ticks is None:
        return "soon"
    secs = int(round(ticks * tick_seconds))
    return f"~{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"~{secs}s"


def briefing(snapshot: dict, tick_seconds: float) -> dict:
    """One-paragraph situation report for the top of the dashboard."""
    services = snapshot["services"]
    rca = snapshot.get("rca")
    plan = snapshot.get("plan")
    summary = snapshot["summary"]
    avail = snapshot["availability"]
    if not snapshot.get("incident"):
        return {"level": "nominal",
                "text": f"All {len(services)} services are within their learned baselines. "
                        f"Serving {snapshot['ingress']:.0f} req/s at {avail * 100:.2f}% availability. "
                        "No failures predicted in the next 15 ticks."}
    parts = []
    inc = snapshot["incident"]
    recent = [a for a in inc.get("actions", []) if snapshot["tick"] - a["tick"] <= 40]
    if recent:
        a = recent[-1]
        who = "Autopilot" if a["source"] == "autopilot" else "Operator"
        parts.append(f"{who} applied {a['label']} at t{a['tick']} for {inc.get('root')} "
                     f"({(inc.get('label') or '').lower()}). Monitoring recovery: availability "
                     f"{avail * 100:.1f}%.")
        failed = summary["failed"]
        if failed:
            parts.append("Still failing: " + ", ".join(failed[:4]) + ".")
        elif snapshot.get("pending_actions"):
            parts.append("Remediation in progress.")
        else:
            parts.append("No failures; waiting for metrics to return to baseline.")
        level = "outage" if avail < 0.5 else "critical" if failed else "degraded"
        return {"level": level, "text": " ".join(parts)}
    if not rca:
        anomalous = [n for n, s in services.items() if s["anomalous"]]
        parts.append("Anomalies detected on " + (", ".join(anomalous[:4]) or "several services") +
                     " - correlating logs and metrics to locate the root cause.")
    else:
        parts.append(f"{rca['root']}: {rca['label'].lower()} ({rca['confidence'] * 100:.0f}% confidence)"
                     + (f" - {rca['evidence'][0]}." if rca["evidence"] else "."))
    failed = summary["failed"]
    if failed:
        parts.append("Failing now: " + ", ".join(failed[:4]) + (" and others." if len(failed) > 4 else "."))
    at_risk = summary["at_risk"][:3]
    if at_risk:
        parts.append("Predicted to fail next: " + ", ".join(
            f"{n} ({_eta_txt(services[n]['eta'], tick_seconds)})" for n in at_risk) + ".")
    crash = summary["system_crash_risk"]
    if ALERT <= crash < 1.0:
        parts.append(f"System-wide outage risk {crash * 100:.0f}% within "
                     f"{_eta_txt(summary.get('system_crash_eta'), tick_seconds)}.")
    elif crash >= 1.0:
        parts.append(f"User-facing availability is down to {avail * 100:.1f}%.")
    if plan:
        best = next(s for s in plan["strategies"] if s["id"] == plan["best"])
        base = next(s for s in plan["strategies"] if s["id"] == plan["baseline"])
        if best["actions"]:
            parts.append(f"Recommended: {best['label']} - projected availability "
                         f"{best['availability'] * 100:.1f}% vs {base['availability'] * 100:.1f}% with no action.")
        else:
            parts.append(plan["headline"])
    if snapshot.get("pending_actions"):
        parts.append("Remediation in progress.")
    level = "outage" if avail < 0.5 else "critical" if failed or crash >= ALERT else "degraded"
    return {"level": level, "text": " ".join(parts)}


def postmortem(inc: dict, timeline: list[dict], tick_seconds: float, generated_tick: int) -> str:
    """Markdown post-mortem for one incident."""
    def t(tick):
        secs = int(tick * tick_seconds)
        return f"t{tick} ({secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d})"

    opened, closed = inc["opened"], inc.get("closed")
    end = closed if closed is not None else generated_tick
    duration = (end - opened) * tick_seconds
    diag = inc.get("diagnosis") or {}
    kind = inc.get("kind") or "overload"
    lines = [f"# Incident #{inc['id']} post-mortem - {inc.get('label') or 'under investigation'}", ""]
    lines += [f"*Generated by ORCA at {t(generated_tick)} on {time.strftime('%Y-%m-%d %H:%M')}. "
              f"Status: **{'resolved' if closed is not None else 'OPEN'}**.*", ""]
    lines += ["## Summary", ""]
    root = inc.get("root") or "undetermined"
    summary = (f"Between {t(opened)} and {t(end) if closed is not None else 'now'} ({duration / 60:.1f} min), "
               f"**{root}** suffered **{(inc.get('label') or 'a fault').lower()}**")
    if inc.get("affected"):
        summary += f", which spread to {len(inc['affected'])} other service(s)"
    summary += (f". User-facing availability dropped to **{inc['min_availability'] * 100:.1f}%** "
                f"and up to **{inc['peak_failed']}** services were failing at once.")
    lines += [summary, ""]

    lines += ["## Impact", "",
              "| Metric | Value |", "|---|---|",
              f"| Duration | {duration / 60:.1f} min |",
              f"| Minimum availability | {inc['min_availability'] * 100:.1f}% |",
              f"| Error budget consumed | {inc.get('loss_seconds', 0) / 60:.1f} min of full outage equivalent |",
              f"| Peak services failing | {inc['peak_failed']} |",
              f"| Services affected | {', '.join(inc.get('affected', [])) or '-'} |", ""]

    lines += ["## Detection and prediction", ""]
    lines.append(f"- Anomaly detected at {t(opened)}.")
    if inc.get("first_prediction") is not None:
        first = [n for n, ft in (inc.get("flagged") or {}).items() if ft == inc["first_prediction"]]
        lines.append(f"- First failure prediction at {t(inc['first_prediction'])} "
                     f"for: {', '.join(first[:6]) or '-'}.")
        later = [n for n in inc.get("predicted_services", []) if n not in first]
        if later:
            lines.append(f"- Later also flagged: {', '.join(later[:8])}.")
    if inc.get("first_failure") is not None:
        lines.append(f"- First service failure at {t(inc['first_failure'])}.")
        if inc.get("first_prediction") is not None and inc["first_prediction"] < inc["first_failure"]:
            lead = inc["first_failure"] - inc["first_prediction"]
            lines.append(f"- **Lead time: {lead} ticks ({lead * tick_seconds:.0f} s) of warning before the first failure.**")
    else:
        lines.append("- No service crossed the failure threshold.")
    leads = inc.get("service_leads") or {}
    if leads:
        lines += ["", "| Service | Predicted at | Failed at | Warning |", "|---|---|---|---|"]
        for svc, (p, f) in sorted(leads.items(), key=lambda kv: kv[1][1]):
            lines.append(f"| {svc} | {'t' + str(p) if p is not None else 'not predicted'} | t{f} | "
                         f"{(str(f - p) + ' ticks') if p is not None else '-'} |")
    lines.append("")

    lines += ["## Root cause", ""]
    if diag:
        lines.append(f"**{diag.get('root')}** - {diag.get('label')} "
                     f"(confidence {diag.get('confidence', 0) * 100:.0f}%).")
        lines.append("")
        lines.append("Evidence:")
        lines += [f"- {e}" for e in diag.get("evidence", [])]
        if diag.get("ranking"):
            lines.append("")
            lines.append("Candidate ranking: " + ", ".join(
                f"{r['service']} {r['score'] * 100:.0f}%" for r in diag["ranking"][:5]) + ".")
    else:
        lines.append("ORCA did not reach a diagnosis for this incident.")
    lines.append("")

    lines += ["## Remediation", ""]
    if inc.get("actions"):
        for a in inc["actions"]:
            who = "ORCA autopilot" if a["source"] == "autopilot" else "Operator"
            proj = ""
            if a.get("projected") is not None:
                proj = (f" - projected availability {a['projected'] * 100:.1f}% "
                        f"vs {a['baseline'] * 100:.1f}% with no action")
            lines.append(f"- {t(a['tick'])}: {who} applied **{a['label']}**{proj}.")
        if closed is not None:
            first_action = inc["actions"][0]["tick"]
            lines.append(f"- Recovered at {t(closed)}, {(closed - first_action) * tick_seconds / 60:.1f} min "
                         "after the first remediation.")
    else:
        lines.append("- No remediation was applied" + (" - the system recovered on its own." if closed else "."))
    if inc.get("escalated") is not None:
        lines.append(f"- {t(inc['escalated'])}: autopilot escalated to on-call (a repeated remediation "
                     "had not resolved the incident).")
    lines.append("")

    lines += ["## Timeline", ""]
    for e in timeline:
        if opened - 3 <= e["tick"] <= end:
            lines.append(f"- {t(e['tick'])} - {e['type'].upper()}: {e['text']}")
    lines.append("")

    lines += ["## Prevention", ""]
    lines += [f"- {p}" for p in PREVENTION.get(kind, PREVENTION["overload"])]
    lines += ["", f"_Fault class: {FAULT_LABELS.get(kind, kind)}._"]
    return "\n".join(lines) + "\n"
