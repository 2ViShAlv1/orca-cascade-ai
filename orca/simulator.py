"""Discrete-time simulator of a microservice estate.

It plays two roles:

* the *live system* that ORCA monitors (it emits metrics and raw log lines and
  is where chaos faults are injected), and
* the *digital twin* the recovery planner forks to run what-if experiments.

Failure physics that make cascades emerge naturally:

* queueing   - latency grows non-linearly with CPU utilisation,
* saturation - in-flight requests = rps x latency (Little's law); a slow
               dependency exhausts the caller's worker / connection pool,
* timeouts   - dependency latency above the client timeout becomes errors,
* retries    - callers retry failed calls, amplifying load on a sick service,
* cache miss - a cold or dead cache multiplies database load,
* crashes    - OOM and failed liveness probes take pods down (crash loops).

All metric updates use the previous tick's dependency state, so a failure
travels one hop per tick or slower - which is what makes it predictable.
"""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field

from .topology import Topology

BASE_HIT_RATIO = 0.85
PORTS = {"cache": 6379, "db": 5432, "search": 9200, "queue": 9092}
HEADROOM = {"edge": 0.2, "service": 0.25}  # pool sized for this utilisation at baseline; others 0.2
FAILED_ERR = 0.35  # a service is "failed" when it is down or its error rate exceeds this

FAULT_CATALOG = {
    "memory_leak": {
        "label": "Memory leak (bad release)",
        "description": "A new release leaks heap memory until pods are OOM-killed in a crash loop.",
    },
    "traffic_surge": {
        "label": "Traffic surge",
        "description": "Ingress traffic ramps to ~2.2x baseline over a few minutes (flash sale / bot storm).",
    },
    "db_slow_queries": {
        "label": "Database lock contention",
        "description": "Long-running queries hold locks; query latency climbs until the connection pool is exhausted.",
    },
    "cache_crash": {
        "label": "Cache memory exhaustion",
        "description": "Redis memory bloats (oversized keys); evictions crush the hit ratio, databases absorb the misses, then Redis is OOM-killed.",
    },
    "bad_deploy": {
        "label": "Faulty deployment",
        "description": "A release ships a bug that throws on a fraction of requests.",
    },
    "disk_fill": {
        "label": "Disk filling up",
        "description": "Log/segment retention is broken; disk usage grows until writes fail.",
    },
}

ACTION_CATALOG = {
    "restart": {"label": "Restart", "risk": 0.15,
                "description": "Rolling restart (stateful: brief outage). Clears memory, kills stuck queries, revives crashed processes."},
    "scale_out": {"label": "Scale out", "risk": 0.05,
                  "description": "Add replicas (ready after ~4 ticks)."},
    "rollback": {"label": "Roll back release", "risk": 0.1,
                 "description": "Redeploy the previous version (rolling, ~4 ticks)."},
    "circuit_breaker": {"label": "Open circuit breaker", "risk": 0.2,
                        "description": "Callers fail fast / use fallbacks instead of waiting on the service, letting it drain."},
    "rate_limit": {"label": "Rate-limit ingress", "risk": 0.3,
                   "description": "Token-bucket ingress cap (excess gets HTTP 429); lifts itself once ingress falls back below the cap."},
    "purge_keys": {"label": "Flush oversized keys", "risk": 0.05,
                   "description": "Find and UNLINK oversized keys (redis-cli --bigkeys) and fix the writer: frees memory without a cold restart."},
    "failover": {"label": "Fail over database", "risk": 0.35,
                 "description": "Promote the standby replica (3 ticks of downtime)."},
    "purge_storage": {"label": "Purge & expand storage", "risk": 0.05,
                      "description": "Fix retention, purge old segments and expand the volume."},
}


def fault_targets(topo: Topology, kind: str) -> list[str]:
    s = topo.services
    if kind in ("memory_leak", "bad_deploy"):
        return [n for n in topo.names if not s[n].stateful]
    if kind == "traffic_surge":
        return [n for n in topo.names if s[n].kind == "edge"]
    if kind == "db_slow_queries":
        return [n for n in topo.names if s[n].kind == "db"]
    if kind == "cache_crash":
        return [n for n in topo.names if s[n].kind == "cache"]
    if kind == "disk_fill":
        return [n for n in topo.names if s[n].has_disk]
    return []


def action_targets(topo: Topology, kind: str) -> list[str]:
    s = topo.services
    if kind == "restart":
        return list(topo.names)
    if kind in ("scale_out", "rollback"):
        return [n for n in topo.names if not s[n].stateful]
    if kind == "circuit_breaker":
        return [n for n in topo.names if topo.callers[n]]
    if kind == "rate_limit":
        return [n for n in topo.names if s[n].kind == "edge"]
    if kind == "failover":
        return [n for n in topo.names if s[n].kind == "db"]
    if kind == "purge_keys":
        return [n for n in topo.names if s[n].kind == "cache"]
    if kind == "purge_storage":
        return [n for n in topo.names if s[n].has_disk]
    return []


@dataclass
class Fault:
    kind: str
    target: str
    start: int
    severity: float = 1.0
    progress: int = 0  # ticks since start (or since last relief action)
    id: int = 0


@dataclass
class ServiceState:
    replicas: int
    threads: float = 1e9
    rps: float = 0.0
    err: float = 0.0
    lat: float = 0.0
    cpu: float = 0.1
    mem: float = 0.45
    sat: float = 0.0
    util: float = 0.0
    disk: float = 0.0
    reject: float = 0.0       # fraction of offered load rejected last tick
    leak: float = 0.0         # memory leaked so far
    stress: int = 0           # consecutive ticks of severe overload
    down_left: int = 0        # >0 => container down / restarting
    crashes: int = 0
    rolling_left: int = 0
    rolling_factor: float = 1.0
    hit_ratio: float = BASE_HIT_RATIO
    version: str = "v2.3.0"
    pending: int = 0          # replicas being provisioned

    @property
    def down(self) -> bool:
        return self.down_left > 0


def _sigmoid(x: float) -> float:
    if x < -30:
        return 0.0
    if x > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def queue_factor(u: float) -> float:
    u = min(max(u, 0.0), 0.97)
    return 1.0 + 0.3 * u / (1.0 - u)


class Simulator:
    def __init__(self, topo: Topology | None = None, seed: int = 7, emit_logs: bool = True,
                 noise: bool = True, start_ts: float = 1792900000.0):
        self.topo = topo or Topology()
        self.rng = random.Random(seed)
        self.emit_logs = emit_logs
        self.noise = noise
        self.tick = 0
        self.start_ts = start_ts
        self.tick_seconds = 5.0
        self.faults: list[Fault] = []
        self._fault_seq = 0
        self.scheduled: list[dict] = []
        self.breakers: dict[str, dict] = {}
        self.rate_limit: dict | None = None
        self.events: list[dict] = []      # observable change events (deploys, actions, crashes)
        self.truth: list[dict] = []       # ground truth (injected faults) - never read by the analyser
        self.logs: list[dict] = []        # logs emitted during the last tick
        self.ingress = 0.0
        self.admitted = 0.0
        self.availability = 1.0
        self.state: dict[str, ServiceState] = {}
        for name, spec in self.topo.services.items():
            minor = self.rng.randint(0, 9)
            st = ServiceState(replicas=spec.replicas, version=f"v2.{self.rng.randint(1, 6)}.{minor}")
            if spec.has_disk:
                st.disk = 0.5 + self.rng.random() * 0.1
            self.state[name] = st
        self._calibrate()

    # ------------------------------------------------------------------ setup
    def _calibrate(self) -> None:
        """Size worker/connection pools for the baseline load (noise-free)."""
        saved = (self.noise, self.emit_logs, self.rng.getstate())
        self.noise, self.emit_logs = False, False
        for _ in range(12):
            self.step()
        for name, st in self.state.items():
            spec = self.topo.services[name]
            inflight = (st.rps / max(st.replicas, 1)) * st.lat / 1000.0
            st.threads = max(inflight / HEADROOM.get(spec.kind, 0.2), 0.5)
        for _ in range(6):
            self.step()
        self.noise, self.emit_logs = saved[0], saved[1]
        self.rng.setstate(saved[2])
        self.tick = 0
        self.events.clear()
        self.logs.clear()

    # ----------------------------------------------------------------- chaos
    def inject(self, kind: str, target: str, severity: float = 1.0) -> Fault:
        if kind not in FAULT_CATALOG:
            raise ValueError(f"unknown fault {kind}")
        if target not in fault_targets(self.topo, kind):
            raise ValueError(f"{kind} cannot target {target}")
        self._fault_seq += 1
        f = Fault(kind, target, self.tick, max(0.3, min(severity, 2.0)), 0, self._fault_seq)
        self.faults.append(f)
        self.truth.append({"tick": self.tick, "kind": kind, "target": target, "severity": f.severity})
        st = self.state[target]
        if kind in ("memory_leak", "bad_deploy"):
            major, minor, patch = st.version[1:].split(".")
            prev = st.version
            st.version = f"v{major}.{minor}.{int(patch) + 1}"
            self.events.append({"tick": self.tick, "type": "deploy", "service": target,
                                "version": st.version, "previous": prev})
            self._log("INFO", target, f"Deployment {target} {st.version} rolled out "
                                      f"({st.replicas}/{st.replicas} replicas ready, previous {prev})")
        return f

    def clear_faults(self) -> None:
        self.faults.clear()

    # ---------------------------------------------------------------- actions
    def apply_action(self, kind: str, target: str | None = None, **params) -> dict:
        topo = self.topo
        if kind not in ACTION_CATALOG:
            raise ValueError(f"unknown action {kind}")
        if kind == "rate_limit" and target is None:
            target = next(n for n in topo.names if topo.services[n].kind == "edge")
        if target not in action_targets(topo, kind):
            raise ValueError(f"{kind} is not applicable to {target}")
        t = self.tick
        st = self.state[target]
        spec = topo.services[target]
        detail = ""
        if kind == "restart":
            if spec.stateful or st.replicas == 1 or st.down:
                st.down_left = 2 if st.down_left > 10 ** 5 else max(st.down_left, 2)
                self.scheduled.append({"due": t + 2, "op": "restart_done", "target": target})
                detail = "full restart (~2 ticks downtime)"
            else:
                st.rolling_left, st.rolling_factor = 3, 0.5
                self.scheduled.append({"due": t + 3, "op": "restart_done", "target": target})
                detail = "rolling restart (50% capacity for 3 ticks)"
        elif kind == "scale_out":
            count = int(params.get("count") or max(1, math.ceil(st.replicas * 0.75)))
            st.pending += count
            self.scheduled.append({"due": t + 4, "op": "scale_done", "target": target, "count": count})
            detail = f"+{count} replicas"
        elif kind == "rollback":
            st.rolling_left, st.rolling_factor = 4, 0.7
            self.scheduled.append({"due": t + 4, "op": "rollback_done", "target": target})
            detail = "rolling back to previous release"
        elif kind == "circuit_breaker":
            self.breakers[target] = {"opened": t, "healthy": 0, "until": t + 40}
            detail = "callers fail fast for up to 40 ticks"
        elif kind == "rate_limit":
            max_rps = float(params.get("max_rps", spec.ingress * 1.2))
            self.rate_limit = {"max_rps": max_rps, "until": t + 300, "calm": 0}
            detail = f"capping ingress at {max_rps:.0f} req/s until the surge subsides"
        elif kind == "failover":
            st.down_left = max(st.down_left, 3)
            self.scheduled.append({"due": t + 3, "op": "failover_done", "target": target})
            detail = "promoting standby (3 ticks)"
        elif kind == "purge_keys":
            if st.down:
                raise ValueError(f"{target} is down - restart it instead")
            self.scheduled.append({"due": t + 2, "op": "purge_keys_done", "target": target})
            detail = "unlinking oversized keys (cache stays warm)"
        elif kind == "purge_storage":
            self.scheduled.append({"due": t + 2, "op": "purge_done", "target": target})
            detail = "purging segments and expanding volume"
        ev = {"tick": t, "type": "action", "action": kind, "service": target, "detail": detail}
        self.events.append(ev)
        self._log("WARN", target, f"Remediation: {ACTION_CATALOG[kind]['label']} on {target} - {detail}")
        return ev

    def _process_scheduled(self) -> None:
        due = [s for s in self.scheduled if s["due"] <= self.tick]
        self.scheduled = [s for s in self.scheduled if s["due"] > self.tick]
        for s in due:
            target, op = s["target"], s["op"]
            st = self.state[target]
            if op == "restart_done":
                st.leak, st.stress = 0.0, 0
                self.faults = [f for f in self.faults if not (f.target == target and f.kind == "cache_crash")]
                if st.down_left > 10 ** 5:
                    st.down_left = 0
                if self.topo.services[target].kind == "cache":
                    st.hit_ratio = 0.25
                for f in self.faults:
                    if f.target == target and f.kind == "db_slow_queries":
                        f.progress = 0  # long-running queries were killed; contention rebuilds
                self._log("INFO", target, f"{target} restarted successfully, health checks passing")
            elif op == "scale_done":
                st.replicas += s["count"]
                st.pending = max(0, st.pending - s["count"])
                self._log("INFO", target, f"HorizontalPodAutoscaler: {target} scaled to {st.replicas} replicas")
            elif op == "rollback_done":
                before = len(self.faults)
                self.faults = [f for f in self.faults
                               if not (f.target == target and f.kind in ("memory_leak", "bad_deploy"))]
                st.leak, st.stress = 0.0, 0
                prev = next((e["previous"] for e in reversed(self.events)
                             if e["type"] == "deploy" and e["service"] == target), st.version)
                st.version = prev
                self.events.append({"tick": self.tick, "type": "deploy", "service": target,
                                    "version": prev, "previous": None, "rollback": True})
                self._log("INFO", target, f"Rollback of {target} to {prev} complete"
                                          + ("" if before != len(self.faults) else " (no change in behaviour expected)"))
            elif op == "failover_done":
                self.faults = [f for f in self.faults if not (f.target == target and f.kind == "db_slow_queries")]
                st.stress = 0
                self._log("INFO", target, f"Standby promoted: {target} primary is now replica-1")
            elif op == "purge_keys_done":
                self.faults = [f for f in self.faults if not (f.target == target and f.kind == "cache_crash")]
                st.leak = 0.0
                st.hit_ratio = min(st.hit_ratio, 0.75)  # the flushed keys are re-fetched once
                self._log("INFO", target, f"Unlinked oversized keys on {target}; used_memory back to normal")
            elif op == "purge_done":
                self.faults = [f for f in self.faults if not (f.target == target and f.kind == "disk_fill")]
                st.disk = 0.45
                self._log("INFO", target, f"Retention fixed, volume expanded: {target} disk at 45%")

    # ------------------------------------------------------------------- step
    def _noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma) if self.noise else 0.0

    def _surge_factor(self) -> float:
        factor = 1.0
        for f in self.faults:
            if f.kind == "traffic_surge":
                peak = 1.0 + 1.2 * f.severity
                ramp = min(1.0, f.progress / 40.0)
                fade = 1.0 if f.progress < 220 else max(0.0, 1.0 - (f.progress - 220) / 20.0)
                factor *= 1.0 + (peak - 1.0) * ramp * fade
        return factor

    def edge_error(self, dep, prev: dict) -> float:
        """Error probability a caller observes when calling `dep.callee` (previous tick)."""
        p = prev[dep.callee]
        if p["down"]:
            return 1.0
        timeouts = max(0.0, _sigmoid((p["lat"] - dep.timeout) / (0.1 * dep.timeout)) - 0.02)
        return 1.0 - (1.0 - p["err"]) * (1.0 - timeouts)

    def cache_miss_multiplier(self, dep, prev: dict) -> float:
        if not dep.via_cache:
            return 1.0
        c = prev[dep.via_cache]
        cache_err = 1.0 if c["down"] else c["err"]
        hit = self.effective_hit_ratio(dep.via_cache) * (1.0 - cache_err)
        return (1.0 - hit) / (1.0 - BASE_HIT_RATIO)

    def effective_hit_ratio(self, cache: str) -> float:
        """Warm-up limited hit ratio, further capped by evictions under memory pressure."""
        st = self.state[cache]
        evict_cap = BASE_HIT_RATIO - max(0.0, st.mem - 0.72) * 2.6
        return max(0.15, min(st.hit_ratio, evict_cap))

    def step(self) -> list[dict]:
        self.tick += 1
        self.logs = []
        self._process_scheduled()
        for f in self.faults:
            f.progress += 1
        self.faults = [f for f in self.faults if not (f.kind == "traffic_surge" and f.progress > 240)]
        if self.rate_limit and (self.tick > self.rate_limit["until"] or self.rate_limit["calm"] >= 10):
            self.rate_limit = None
            self._log("INFO", "api-gateway", "Rate limiter disabled, admitting 100% of ingress")

        topo = self.topo
        prev = {n: {"err": s.err, "lat": s.lat, "down": s.down, "reject": s.reject}
                for n, s in self.state.items()}

        # 1. ingress and top-down load propagation
        diurnal = 1.0 + 0.06 * math.sin(2 * math.pi * self.tick / 300.0)
        load = {n: 0.0 for n in topo.names}
        self.ingress = 0.0
        for n, spec in topo.services.items():
            if spec.ingress:
                offered = spec.ingress * diurnal * (1.0 + self._noise(0.02))
                if spec.kind == "edge":
                    offered *= self._surge_factor()
                    self.ingress = offered
                    self.admitted = min(offered, self.rate_limit["max_rps"]) if self.rate_limit else offered
                    load[n] += self.admitted
                else:
                    load[n] += offered
        for n in topo.order:
            served = 0.0 if prev[n]["down"] else load[n] * (1.0 - prev[n]["reject"])
            for d in topo.callees[n]:
                calls = d.calls * self.cache_miss_multiplier(d, prev)
                if d.callee in self.breakers:
                    calls *= 0.05
                    retry = 1.0
                else:
                    e = self.edge_error(d, prev)
                    retry = 1.0 + e + e * e
                load[d.callee] += served * calls * retry

        # 2. per-service dynamics
        for n in topo.names:
            self._update_service(n, load[n], prev)

        # 3. breakers, cache warm-up, availability
        for name in list(self.breakers):
            b = self.breakers[name]
            s = self.state[name]
            b["healthy"] = b["healthy"] + 1 if (not s.down and s.err < 0.08 and s.sat < 0.9) else 0
            if b["healthy"] >= 3 or self.tick >= b["until"]:
                del self.breakers[name]
                self._log("INFO", name, f"CircuitBreaker '{name}' HALF_OPEN -> CLOSED (probe requests succeeded)")
        for n, spec in topo.services.items():
            st = self.state[n]
            if spec.kind == "cache" and not st.down and st.hit_ratio < BASE_HIT_RATIO:
                st.hit_ratio = min(BASE_HIT_RATIO, st.hit_ratio + 0.06)
        gw = next(n for n in topo.names if topo.services[n].kind == "edge")
        g = self.state[gw]
        gw_ok = 0.0 if g.down else (1.0 - g.err)
        self.availability = gw_ok * (self.admitted / self.ingress if self.ingress else 1.0)
        if self.rate_limit:  # adaptive: count ticks with ingress comfortably below the cap
            rl = self.rate_limit
            rl["calm"] = rl["calm"] + 1 if self.ingress < 0.9 * rl["max_rps"] else 0
        if self.emit_logs:
            self._emit_logs(prev)
        return self.logs

    def _update_service(self, n: str, load: float, prev: dict) -> None:
        spec = self.topo.services[n]
        st = self.state[n]
        st.rps = load * (1.0 + self._noise(0.015))
        if st.down:
            if st.down_left < 10 ** 5:
                st.down_left -= 1
                if st.down_left == 0:
                    st.leak, st.stress = 0.0, 0
                    if spec.kind == "cache":
                        st.hit_ratio = 0.25
                    self._log("INFO", n, f"Container {n} started, readiness probe passed")
            st.err, st.lat, st.cpu, st.sat, st.util, st.reject = 1.0, 2.0, 0.02, 0.0, 0.0, 1.0
            st.mem = 0.05
            return

        lat_mult, cpu_add, own_err = 1.0, 0.0, 0.0
        for f in self.faults:
            if f.target != n:
                continue
            if f.kind == "memory_leak":
                st.leak += 0.011 * f.severity
            elif f.kind == "db_slow_queries":
                ramp = min(1.0, f.progress / 45.0)
                lat_mult *= 1.0 + 9.0 * f.severity * ramp
                cpu_add += 0.25 * ramp
            elif f.kind == "bad_deploy":
                rollout = min(1.0, f.progress / 15.0)  # canary -> full rollout
                own_err = 1.0 - (1.0 - own_err) * (1.0 - min(0.8, 0.4 * f.severity * rollout))
                lat_mult *= 1.0 + 0.3 * rollout
            elif f.kind == "cache_crash":
                st.leak += 0.012 * f.severity
            elif f.kind == "disk_fill":
                st.disk = min(1.0, st.disk + 0.007 * f.severity)
        if spec.has_disk and st.disk >= 0.98:
            own_err = 1.0 - (1.0 - own_err) * 0.15

        factor = st.rolling_factor if st.rolling_left > 0 else 1.0
        if st.rolling_left > 0:
            st.rolling_left -= 1
        capacity = max(st.replicas * spec.capacity * factor, 1e-6)
        u = load / capacity
        mem = 0.42 + 0.12 * min(u, 1.2) + st.leak + self._noise(0.006)
        gc = 1.0 + max(0.0, mem - 0.82) / 0.18 * 3.0
        own_lat = spec.base_latency * queue_factor(u) * lat_mult * gc

        dep_lat = 0.0
        success = 1.0
        for d in self.topo.callees[n]:
            calls = d.calls * self.cache_miss_multiplier(d, prev)
            p = prev[d.callee]
            if d.callee in self.breakers:
                e = 1.0 if d.hard else 0.0
                dep_lat += 0.05 * calls * min(p["lat"], d.timeout)
            else:
                e = self.edge_error(d, prev)
                dep_lat += calls * (2.0 if p["down"] else min(p["lat"], d.timeout))
            if d.hard:
                fail = min(1.0, calls * e) if calls <= 1.0 else 1.0 - (1.0 - e) ** calls
                success *= 1.0 - fail

        lat_unqueued = own_lat + dep_lat
        replicas_ready = max(st.replicas * factor, 0.5)
        inflight = (load / replicas_ready) * lat_unqueued / 1000.0
        sat = inflight / st.threads
        lat = min(30000.0, lat_unqueued * (1.0 + 1.5 * max(0.0, sat - 1.0)))
        shed = max(0.0, 1.0 - 1.0 / u) if u > 1.0 else 0.0
        reject = max(0.0, 1.0 - 1.0 / sat) if sat > 1.0 else 0.0
        success *= (1.0 - shed) * (1.0 - reject) * (1.0 - own_err)
        err_target = 1.0 - success

        st.err = min(1.0, max(0.0, st.err + 0.6 * (err_target - st.err) + self._noise(0.002)))
        st.lat = max(0.5, st.lat + 0.6 * (lat - st.lat)) * (1.0 + self._noise(0.03))
        cpu_target = min(1.0, 0.05 + 0.85 * min(u, 1.15) + cpu_add + (gc - 1.0) * 0.08)
        st.cpu = min(1.0, max(0.0, st.cpu + 0.5 * (cpu_target - st.cpu) + self._noise(0.012)))
        st.sat = max(0.0, st.sat + 0.6 * (min(sat, 5.0) - st.sat))
        st.util = u
        st.mem = min(1.0, max(0.0, mem))
        st.reject = max(shed, reject)

        if u > 1.6 or sat > 2.2:
            st.stress += 1
        else:
            st.stress = max(0, st.stress - 1)
        if mem >= 1.0:
            self._crash(n, "OOMKilled: container exceeded memory limit (2Gi), restarting (CrashLoopBackOff)")
        elif st.stress >= 6:
            self._crash(n, "Liveness probe failed: HTTP probe failed with statuscode 503 "
                           "(timeout 1s); killing container")

    def _crash(self, n: str, reason: str) -> None:
        st = self.state[n]
        st.down_left = 4
        st.crashes += 1
        st.stress = 0
        st.rolling_left = 0
        self.events.append({"tick": self.tick, "type": "crash", "service": n, "reason": reason})
        self._log("FATAL", n, reason)

    # ------------------------------------------------------------------- logs
    def _log(self, level: str, service: str, msg: str) -> None:
        if not self.emit_logs:
            return
        self.logs.append({"tick": self.tick, "ts": self.start_ts + self.tick * self.tick_seconds,
                          "level": level, "service": service, "msg": msg})

    def _emit_logs(self, prev: dict) -> None:
        rng = self.rng
        topo = self.topo

        def n_lines(rate: float, cap: int = 3) -> int:
            # Poisson draw (Knuth) capped to keep the stream readable
            if rate <= 0:
                return 0
            L, k, p = math.exp(-min(rate, 20)), 0, 1.0
            while True:
                p *= rng.random()
                if p <= L:
                    break
                k += 1
            return min(k, cap)

        for n, spec in topo.services.items():
            st = self.state[n]
            if st.down:
                continue
            port = PORTS.get(spec.kind, 8080)
            # dependency failures (blame pointers in text)
            for d in topo.callees[n]:
                if d.callee in self.breakers:
                    if rng.random() < 0.15:
                        self._log("WARN", n, f"CircuitBreaker '{d.callee}' is OPEN - "
                                             f"{'failing fast' if d.hard else 'serving fallback'}")
                    continue
                e = self.edge_error(d, prev)
                if e < 0.04:
                    continue
                cport = PORTS.get(topo.services[d.callee].kind, 8080)
                for _ in range(n_lines(e * 5 * min(1.0, d.calls + 0.3))):
                    if prev[d.callee]["down"]:
                        msg = f"Connection refused: {d.callee}:{cport} (ECONNREFUSED)"
                    elif prev[d.callee]["lat"] > 0.8 * d.timeout:
                        msg = f"Timeout after {int(d.timeout)}ms waiting for response from {d.callee}"
                    else:
                        msg = f"Call to {d.callee} failed: 503 Service Unavailable"
                    if d.hard:
                        self._log("ERROR", n, msg)
                    else:
                        self._log("WARN", n, msg + "; fallback response served")
            # own saturation / overload
            if st.reject > 0.03:
                for _ in range(n_lines(st.reject * 4)):
                    if spec.kind == "db":
                        self._log("ERROR", n, f"FATAL: remaining connection slots are reserved; "
                                              f"too many clients already (load {st.rps:.0f} q/s)")
                    else:
                        self._log("ERROR", n, f"Request rejected: worker pool exhausted "
                                              f"(utilisation {min(st.sat, 9.99) * 100:.0f}%, queue full)")
            elif st.sat > 0.8 and rng.random() < 0.5:
                self._log("WARN", n, f"Worker pool utilisation {st.sat * 100:.0f}% - requests queueing")
            # memory pressure
            if st.mem > 0.8 and rng.random() < (st.mem - 0.75) * 4:
                if st.mem > 0.93:
                    self._log("ERROR", n, "java.lang.OutOfMemoryError: GC overhead limit exceeded")
                else:
                    pause = int(80 + (st.mem - 0.8) * 4000)
                    self._log("WARN", n, f"GC pause {pause}ms (G1 Evacuation Pause), heap {st.mem * 100:.0f}% after collection")
            # cache evictions
            if spec.kind == "cache" and st.mem > 0.74 and rng.random() < 0.6:
                hit = self.effective_hit_ratio(n)
                self._log("WARN", n, f"used_memory {st.mem * 100:.0f}% of maxmemory - evicted "
                                     f"{int((st.mem - 0.7) * 90000)} keys/s, hit ratio {hit * 100:.0f}%")
            # database slow queries
            if spec.kind == "db":
                ratio = st.lat / max(spec.base_latency, 1)
                if ratio > 3 and rng.random() < min(0.9, ratio / 12):
                    tbl = {"users-db": "sessions", "product-db": "products", "orders-db": "orders"}.get(n, "t")
                    self._log("WARN" if ratio < 8 else "ERROR", n,
                              f"Slow query ({st.lat * 3:.0f}ms): UPDATE {tbl} SET ... WHERE id = $1 - "
                              f"waiting for ShareLock on transaction")
            # faulty release
            for f in self.faults:
                if f.target == n and f.kind == "bad_deploy":
                    for _ in range(n_lines(4 * f.severity, 2)):
                        cls = n.split("-")[0].capitalize()
                        self._log("ERROR", n, f"Unhandled exception in {cls}Controller.handle: "
                                              f"NullPointerException at {cls}Mapper.toDto({cls}Mapper.java:"
                                              f"{rng.randint(40, 220)}) [build {st.version}]")
            # disk
            if spec.has_disk and st.disk > 0.85 and rng.random() < 0.35 + (st.disk - 0.85) * 3:
                if st.disk >= 0.98:
                    self._log("ERROR", n, "IOException: No space left on device - segment write failed")
                else:
                    self._log("WARN", n, f"Disk usage {st.disk * 100:.0f}% on /var/lib/{n} exceeds 85% watermark")
            # latency SLO
            if spec.kind in ("edge", "service") and st.lat > 1200 and rng.random() < 0.3:
                self._log("WARN", n, f"Slow response {st.lat:.0f}ms on GET /api/v1/{n.split('-')[0]} (SLO 1000ms)")
            # ingress surge
            if spec.kind == "edge" and spec.ingress and rng.random() < 0.3:
                base = spec.ingress
                if self.ingress > 1.3 * base:
                    self._log("WARN", n, f"Ingress {self.ingress:.0f} req/s is {self.ingress / base * 100 - 100:.0f}% "
                                         f"above 7-day p95 ({base:.0f} req/s)")
                if self.rate_limit and self.ingress > self.admitted * 1.01:
                    self._log("WARN", n, f"Rate limiter shedding {(1 - self.admitted / self.ingress) * 100:.0f}% "
                                         f"of ingress (429 Too Many Requests)")
            # access-log chatter
            if st.err < 0.05 and rng.random() < 0.08:
                if spec.kind in ("edge", "service"):
                    self._log("INFO", n, f"GET /api/v1/{n.split('-')[0]} 200 {st.lat:.0f}ms")
                else:
                    self._log("INFO", n, f"checkpoint complete: {rng.randint(40, 900)} buffers written")

    # --------------------------------------------------------------- outputs
    def observe(self) -> dict[str, dict]:
        out = {}
        for n, st in self.state.items():
            out[n] = {
                "rps": st.rps, "err": st.err, "lat": st.lat, "cpu": st.cpu, "mem": st.mem,
                "sat": st.sat, "util": st.util, "disk": st.disk, "replicas": st.replicas,
                "pending": st.pending, "down": st.down, "crashes": st.crashes,
                "version": st.version, "breaker": n in self.breakers,
                "hit": self.effective_hit_ratio(n) if self.topo.services[n].kind == "cache" else None,
            }
        return out

    def failed(self, name: str) -> bool:
        st = self.state[name]
        return st.down or st.err >= FAILED_ERR

    def fork(self, faults: list[Fault] | None = None, noise: bool = False) -> "Simulator":
        """Copy of the current state for what-if experiments."""
        logs, self.logs = self.logs, []
        twin = copy.deepcopy(self)
        self.logs = logs
        twin.emit_logs = False
        twin.noise = noise
        if faults is not None:
            twin.faults = [copy.copy(f) for f in faults]
        return twin
