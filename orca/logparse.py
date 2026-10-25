"""Log template mining for raw service log lines.

Each line is matched against known templates and reduced to a category,
whether it is *intrinsic* evidence (the service itself is sick) or a
*symptom*, and - for dependency errors - which downstream service the
line blames. The blame pointers feed root-cause analysis.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (template id, category, intrinsic?, regex)
TEMPLATES: list[tuple[str, str, bool, re.Pattern]] = [
    ("dep_refused", "dependency", False, re.compile(r"Connection refused: (?P<dep>[\w-]+):\d+")),
    ("dep_timeout", "dependency", False, re.compile(r"Timeout after \d+ms waiting for response from (?P<dep>[\w-]+)")),
    ("dep_5xx", "dependency", False, re.compile(r"Call to (?P<dep>[\w-]+) failed: 5\d\d")),
    ("pool_exhausted", "overload", False, re.compile(r"worker pool exhausted")),
    ("db_conn_exhausted", "overload", False, re.compile(r"too many clients already")),
    ("pool_high", "overload", False, re.compile(r"Worker pool utilisation \d+%")),
    ("oom_killed", "memory", True, re.compile(r"OOMKilled")),
    ("oom_error", "memory", True, re.compile(r"OutOfMemoryError")),
    ("gc_pause", "memory", True, re.compile(r"GC pause \d+ms")),
    ("cache_evictions", "cache", True, re.compile(r"evicted \d+ keys/s, hit ratio (?P<hit>\d+)%")),
    ("slow_query", "database", True, re.compile(r"Slow query \((?P<ms>\d+)ms\)")),
    ("exception", "code", True, re.compile(r"Unhandled exception .*\[build (?P<ver>v[\d.]+)\]")),
    ("disk_full", "disk", True, re.compile(r"No space left on device")),
    ("disk_high", "disk", True, re.compile(r"Disk usage (?P<pct>\d+)%")),
    ("ingress_surge", "traffic", True, re.compile(r"Ingress (?P<rps>\d+) req/s is (?P<pct>\d+)% above")),
    ("liveness_fail", "crash", False, re.compile(r"Liveness probe failed")),
    ("slow_response", "latency", False, re.compile(r"Slow response (?P<ms>\d+)ms")),
    ("deploy", "change", False, re.compile(r"Deployment (?P<svc>[\w-]+) (?P<ver>v[\d.]+) rolled out")),
    ("remediation", "change", False, re.compile(r"^Remediation:")),
    ("breaker", "resilience", False, re.compile(r"CircuitBreaker")),
    ("rate_limited", "resilience", False, re.compile(r"Rate limiter shedding")),
]


@dataclass
class ParsedLog:
    tick: int
    service: str
    level: str
    template: str
    category: str
    intrinsic: bool
    blame: str | None
    fields: dict


def parse(log: dict) -> ParsedLog:
    msg = log["msg"]
    for tid, cat, intrinsic, rx in TEMPLATES:
        m = rx.search(msg)
        if m:
            fields = m.groupdict()
            return ParsedLog(log["tick"], log["service"], log["level"], tid, cat, intrinsic,
                             fields.get("dep"), fields)
    return ParsedLog(log["tick"], log["service"], log["level"], "other", "info", False, None, {})
