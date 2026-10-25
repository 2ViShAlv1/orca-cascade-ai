"""Random chaos scenarios shared by model training and evaluation."""
from __future__ import annotations

import random

from .simulator import FAULT_CATALOG, fault_targets
from .topology import Topology

# the remediation an expert would pick for each fault (used to diversify training data)
EXPERT_FIX = {
    "memory_leak": "rollback",
    "bad_deploy": "rollback",
    "traffic_surge": "rate_limit",
    "db_slow_queries": "failover",
    "cache_crash": "purge_keys",
    "disk_fill": "purge_storage",
}

PRESETS = [
    {"id": "black-friday", "label": "Black Friday flash sale", "faults": [("traffic_surge", "api-gateway", 1.1)]},
    {"id": "leaky-release", "label": "Leaky catalog release", "faults": [("memory_leak", "catalog-service", 1.0)]},
    {"id": "lock-storm", "label": "Product DB lock storm", "faults": [("db_slow_queries", "product-db", 1.0)]},
    {"id": "cache-meltdown", "label": "Redis memory meltdown", "faults": [("cache_crash", "redis-cache", 1.0)]},
    {"id": "bad-friday-deploy", "label": "Buggy inventory deploy", "faults": [("bad_deploy", "inventory-service", 1.0)]},
    {"id": "kafka-disk", "label": "Kafka disk exhaustion", "faults": [("disk_fill", "kafka", 1.3)]},
    {"id": "double-trouble", "label": "Surge during DB contention",
     "faults": [("traffic_surge", "api-gateway", 0.7), ("db_slow_queries", "orders-db", 1.0)]},
]


def random_fault(rng: random.Random, topo: Topology) -> tuple[str, str, float]:
    kind = rng.choice(list(FAULT_CATALOG))
    target = rng.choice(fault_targets(topo, kind))
    return kind, target, round(rng.uniform(0.6, 1.5), 2)


def random_episode(rng: random.Random, topo: Topology) -> list[tuple[int, str, str, float]]:
    """Faults as (tick, kind, target, severity)."""
    roll = rng.random()
    if roll < 0.05:
        return []
    first = rng.randint(30, 50)
    plan = [(first, *random_fault(rng, topo))]
    if roll > 0.88:
        kind, target, sev = random_fault(rng, topo)
        if target != plan[0][2]:
            plan.append((first + rng.randint(0, 30), kind, target, sev))
    return plan
