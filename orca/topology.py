"""Service topology: the microservice estate that ORCA monitors.

The topology is a directed acyclic graph of caller -> callee dependencies.
Every edge records how many calls the caller makes to the callee per request
it serves, whether the dependency is *hard* (callee failure fails the request)
or *soft* (caller degrades gracefully with a fallback) and the client timeout.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: str            # edge | service | cache | db | search | queue
    tier: int            # layout layer, 0 = edge
    replicas: int
    capacity: float      # requests/s one replica handles at 100% CPU
    base_latency: float  # ms of own processing at low utilisation
    criticality: float   # business weight of the service (0..1)
    ingress: float = 0.0 # traffic that originates at this service (rps)
    stateful: bool = False
    has_disk: bool = False

    @property
    def scalable(self) -> bool:
        return not self.stateful


@dataclass(frozen=True)
class Dependency:
    caller: str
    callee: str
    calls: float          # calls to callee per request served by caller
    hard: bool            # hard dependency => callee errors fail the request
    timeout: float        # client timeout in ms
    via_cache: str | None = None  # call volume scales with this cache's miss rate


SERVICES: list[ServiceSpec] = [
    ServiceSpec("api-gateway", "edge", 0, 3, 300, 4, 1.0, ingress=400),
    ServiceSpec("auth-service", "service", 1, 4, 220, 8, 0.95),
    ServiceSpec("catalog-service", "service", 1, 3, 130, 15, 0.8),
    ServiceSpec("search-service", "service", 1, 2, 70, 25, 0.6),
    ServiceSpec("cart-service", "service", 1, 2, 90, 10, 0.75),
    ServiceSpec("order-service", "service", 1, 2, 30, 30, 1.0),
    ServiceSpec("recommendation-service", "service", 1, 2, 120, 35, 0.3),
    ServiceSpec("payment-service", "service", 2, 2, 25, 40, 1.0),
    ServiceSpec("inventory-service", "service", 2, 2, 70, 12, 0.8),
    ServiceSpec("fraud-detection", "service", 2, 1, 60, 20, 0.7),
    ServiceSpec("notification-service", "service", 2, 1, 80, 15, 0.3, ingress=30),
    ServiceSpec("redis-cache", "cache", 3, 1, 2000, 1, 0.9, stateful=True),
    ServiceSpec("users-db", "db", 3, 1, 300, 5, 0.95, stateful=True, has_disk=True),
    ServiceSpec("product-db", "db", 3, 1, 320, 6, 0.85, stateful=True, has_disk=True),
    ServiceSpec("orders-db", "db", 3, 1, 180, 8, 1.0, stateful=True, has_disk=True),
    ServiceSpec("elasticsearch", "search", 3, 1, 150, 20, 0.6, stateful=True, has_disk=True),
    ServiceSpec("kafka", "queue", 3, 1, 400, 4, 0.8, stateful=True, has_disk=True),
]

DEPENDENCIES: list[Dependency] = [
    Dependency("api-gateway", "auth-service", 1.0, True, 1000),
    Dependency("api-gateway", "catalog-service", 0.45, True, 3000),
    Dependency("api-gateway", "search-service", 0.15, True, 3000),
    Dependency("api-gateway", "cart-service", 0.2, True, 3000),
    Dependency("api-gateway", "order-service", 0.06, True, 5000),
    Dependency("api-gateway", "recommendation-service", 0.3, False, 800),
    Dependency("auth-service", "redis-cache", 1.0, False, 100),
    Dependency("auth-service", "users-db", 0.3, True, 2000, via_cache="redis-cache"),
    Dependency("catalog-service", "redis-cache", 1.0, False, 100),
    Dependency("catalog-service", "product-db", 0.4, True, 2000, via_cache="redis-cache"),
    Dependency("search-service", "elasticsearch", 1.0, True, 2000),
    Dependency("cart-service", "redis-cache", 1.0, True, 200),
    Dependency("cart-service", "inventory-service", 0.5, True, 1500),
    Dependency("order-service", "payment-service", 1.0, True, 3000),
    Dependency("order-service", "inventory-service", 1.0, True, 1500),
    Dependency("order-service", "orders-db", 2.0, True, 2000),
    Dependency("order-service", "kafka", 1.0, True, 1000),
    Dependency("recommendation-service", "catalog-service", 0.5, False, 500),
    Dependency("recommendation-service", "redis-cache", 1.0, False, 100),
    Dependency("payment-service", "fraud-detection", 1.0, True, 1500),
    Dependency("payment-service", "orders-db", 1.0, True, 2000),
    Dependency("inventory-service", "product-db", 1.0, True, 2000),
    Dependency("fraud-detection", "users-db", 0.5, False, 500),
    Dependency("notification-service", "kafka", 1.0, True, 1000),
]


class Topology:
    """Indexed view over services and dependencies."""

    def __init__(self, services=None, deps=None):
        self.services: dict[str, ServiceSpec] = {s.name: s for s in (services or SERVICES)}
        self.deps: list[Dependency] = list(deps or DEPENDENCIES)
        self.names: list[str] = list(self.services)
        self.callees: dict[str, list[Dependency]] = {n: [] for n in self.names}
        self.callers: dict[str, list[Dependency]] = {n: [] for n in self.names}
        for d in self.deps:
            self.callees[d.caller].append(d)
            self.callers[d.callee].append(d)
        self.order = self._topological_order()  # callers before callees

    def _topological_order(self) -> list[str]:
        indeg = {n: len(self.callers[n]) for n in self.names}
        queue = [n for n in self.names if indeg[n] == 0]
        order: list[str] = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for d in self.callees[n]:
                indeg[d.callee] -= 1
                if indeg[d.callee] == 0:
                    queue.append(d.callee)
        if len(order) != len(self.names):
            raise ValueError("dependency graph has a cycle")
        return order

    def upstream_closure(self, name: str) -> set[str]:
        """All services that (transitively) call `name`."""
        seen: set[str] = set()
        stack = [name]
        while stack:
            for d in self.callers[stack.pop()]:
                if d.caller not in seen:
                    seen.add(d.caller)
                    stack.append(d.caller)
        return seen

    def layout(self) -> dict[str, tuple[float, float]]:
        """Layered coordinates in a 0..1 box (tier = row)."""
        tiers: dict[int, list[str]] = {}
        for s in self.services.values():
            tiers.setdefault(s.tier, []).append(s.name)
        max_tier = max(tiers)
        pos = {}
        for tier, names in tiers.items():
            for i, n in enumerate(names):
                pos[n] = ((i + 0.5) / len(names), (tier + 0.5) / (max_tier + 1))
        return pos

    def to_json(self) -> dict:
        pos = self.layout()
        return {
            "services": [
                {
                    "name": s.name, "kind": s.kind, "tier": s.tier,
                    "replicas": s.replicas, "criticality": s.criticality,
                    "stateful": s.stateful, "x": pos[s.name][0], "y": pos[s.name][1],
                }
                for s in self.services.values()
            ],
            "dependencies": [
                {"caller": d.caller, "callee": d.callee, "hard": d.hard, "calls": d.calls}
                for d in self.deps
            ],
        }
