"""Unit and integration tests: python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca.engine import Engine, Pipeline  # noqa: E402
from orca.logparse import parse  # noqa: E402
from orca.observer import slope, time_to  # noqa: E402
from orca.predictor import GBDTModel, LogisticModel, auc  # noqa: E402
from orca.simulator import Simulator, fault_targets  # noqa: E402
from orca.topology import Topology  # noqa: E402

TOPO = Topology()


class TopologyTests(unittest.TestCase):
    def test_topological_order_callers_first(self):
        pos = {n: i for i, n in enumerate(TOPO.order)}
        for d in TOPO.deps:
            self.assertLess(pos[d.caller], pos[d.callee])

    def test_upstream_closure(self):
        self.assertIn("api-gateway", TOPO.upstream_closure("product-db"))
        self.assertNotIn("kafka", TOPO.upstream_closure("product-db"))


class SimulatorTests(unittest.TestCase):
    def test_baseline_is_healthy(self):
        sim = Simulator(TOPO, seed=3)
        for _ in range(40):
            sim.step()
        self.assertGreater(sim.availability, 0.97)
        self.assertFalse(any(sim.failed(n) for n in TOPO.names))

    def test_memory_leak_crashes_and_rollback_fixes(self):
        sim = Simulator(TOPO, seed=4)
        sim.inject("memory_leak", "catalog-service")
        twin = sim.fork(noise=False)
        crashed = False
        for _ in range(70):
            sim.step()
            crashed |= sim.state["catalog-service"].down
        self.assertTrue(crashed)
        twin.apply_action("rollback", "catalog-service")
        for _ in range(70):
            twin.step()
        self.assertEqual(twin.state["catalog-service"].crashes, 0)
        self.assertFalse(twin.faults)

    def test_cache_exhaustion_cascades_to_databases(self):
        sim = Simulator(TOPO, seed=5)
        sim.inject("cache_crash", "redis-cache")
        db_failed = False
        for _ in range(60):
            sim.step()
            db_failed |= sim.failed("users-db") or sim.failed("product-db")
        self.assertTrue(db_failed)

    def test_invalid_fault_target_rejected(self):
        sim = Simulator(TOPO, seed=1)
        with self.assertRaises(ValueError):
            sim.inject("db_slow_queries", "api-gateway")
        self.assertEqual(fault_targets(TOPO, "traffic_surge"), ["api-gateway"])

    def test_fork_is_independent(self):
        sim = Simulator(TOPO, seed=6)
        twin = sim.fork()
        twin.inject("bad_deploy", "inventory-service")
        for _ in range(5):
            twin.step()
        self.assertFalse(sim.faults)
        self.assertEqual(sim.tick, 0)


class LogParseTests(unittest.TestCase):
    def test_blame_pointer(self):
        p = parse({"tick": 1, "service": "catalog-service", "level": "ERROR",
                   "msg": "Timeout after 2000ms waiting for response from product-db"})
        self.assertEqual(p.template, "dep_timeout")
        self.assertEqual(p.blame, "product-db")
        self.assertFalse(p.intrinsic)

    def test_intrinsic(self):
        p = parse({"tick": 1, "service": "kafka", "level": "ERROR",
                   "msg": "IOException: No space left on device - segment write failed"})
        self.assertEqual(p.category, "disk")
        self.assertTrue(p.intrinsic)


class MathTests(unittest.TestCase):
    def test_slope_and_time_to(self):
        self.assertAlmostEqual(slope([1, 2, 3, 4]), 1.0)
        self.assertAlmostEqual(time_to(0.5, 1.0, 0.05), 10.0)
        self.assertEqual(time_to(0.5, 1.0, 0.0), float("inf"))

    def test_auc(self):
        self.assertAlmostEqual(auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]), 0.75)

    def test_models_learn_simple_rule(self):
        X = [{"a": i / 100.0, "b": (i * 7 % 13) / 13.0} for i in range(200)]
        y = [int(x["a"] > 0.5) for x in X]
        for model in (LogisticModel(["a", "b"]), GBDTModel(["a", "b"])):
            if isinstance(model, GBDTModel):
                model.fit(X, y, n_trees=30, depth=2)
            else:
                model.fit(X, y, epochs=60)
            self.assertGreater(auc([model.predict(x) for x in X], y), 0.95)

    def test_gbdt_contributions_sum_to_margin(self):
        X = [{"a": i / 50.0, "b": (i % 5) / 5.0} for i in range(100)]
        y = [int(x["a"] + x["b"] > 1.2) for x in X]
        m = GBDTModel(["a", "b"])
        m.fit(X, y, n_trees=10, depth=2)
        margin, contrib = m._margin_and_contrib(X[42])
        bias = m.base + sum(t[4] for t in m.trees)
        self.assertAlmostEqual(margin, bias + sum(contrib), places=9)


class PipelineTests(unittest.TestCase):
    def test_no_false_alarm_when_calm(self):
        p = Pipeline(TOPO, seed=21)
        for _ in range(40):
            p.step()
        self.assertFalse(p.summary["at_risk"])
        self.assertIsNone(p.diagnosis)

    def test_db_contention_end_to_end(self):
        p = Pipeline(TOPO, seed=22)
        p.sim.inject("db_slow_queries", "product-db")
        first_flag = first_fail = None
        plan = None
        for _ in range(60):
            p.step()
            t = p.sim.tick
            if first_flag is None and p.summary["at_risk"]:
                first_flag = t
            if first_fail is None and p.summary["failed"]:
                first_fail = t
            if plan is None and p.diagnosis and p.summary["at_risk"]:
                plan = p.plan()
                diag = p.diagnosis
        self.assertIsNotNone(first_flag)
        self.assertLess(first_flag, first_fail)          # predicted before it happened
        self.assertEqual(diag["root"], "product-db")
        self.assertEqual(diag["kind"], "db_slow_queries")
        best = plan["strategies"][0]
        self.assertTrue(best["actions"])
        self.assertGreater(best["availability"], plan["strategies"][-1]["availability"])

    def test_rate_limit_lifts_itself_after_surge(self):
        sim = Simulator(TOPO, seed=31)
        sim.apply_action("rate_limit", "api-gateway", max_rps=480)
        for _ in range(15):
            sim.step()
        self.assertIsNone(sim.rate_limit)          # no surge -> limiter lifts after 10 calm ticks
        sim.inject("traffic_surge", "api-gateway")
        for _ in range(45):
            sim.step()
        sim.apply_action("rate_limit", "api-gateway", max_rps=600)
        for _ in range(80):
            sim.step()
        self.assertIsNotNone(sim.rate_limit)       # surge still running -> limiter stays on

    def test_flush_keys_keeps_cache_warm(self):
        sim = Simulator(TOPO, seed=32)
        sim.inject("cache_crash", "redis-cache")
        for _ in range(20):
            sim.step()
        sim.apply_action("purge_keys", "redis-cache")
        for _ in range(40):
            sim.step()
        self.assertFalse(sim.faults)
        self.assertEqual(sim.state["redis-cache"].crashes, 0)
        self.assertGreater(sim.availability, 0.95)

    def test_autopilot_never_repeats_a_failed_action(self):
        from orca.recovery import should_act
        plan = {"baseline": 1, "strategies": [
            {"id": 0, "label": "Fail over database product-db", "score": 90.0,
             "actions": [{"kind": "failover", "target": "product-db"}]},
            {"id": 1, "label": "No action", "score": 40.0, "actions": []}]}
        diag = {"confidence": 0.9}
        summary = {"failed": ["api-gateway"], "at_risk": []}
        best, why = should_act(plan, diag, summary, 0.6, [], 100)
        self.assertEqual(best["id"], 0)
        best, why = should_act(plan, diag, summary, 0.6,
                               [{"tick": 80, "kind": "failover", "target": "product-db"}], 100)
        self.assertIsNone(best)
        self.assertIn("escalating", why)

    def test_twin_models_secondary_faults(self):
        p = Pipeline(TOPO, seed=33)
        p.sim.inject("traffic_surge", "api-gateway", 0.7)
        p.sim.inject("db_slow_queries", "orders-db", 1.0)
        kinds = set()
        for _ in range(40):
            p.step()
            if p.diagnosis:
                from orca.recovery import estimate_faults
                kinds |= {f.kind for f in estimate_faults(p.obs, p.diagnosis)}
        self.assertEqual(kinds, {"traffic_surge", "db_slow_queries"})

    def test_engine_snapshot_and_commands(self):
        e = Engine(seed=9)
        e.preset("bad-friday-deploy")
        for _ in range(20):
            e.step()
        snap = e.full_state()
        self.assertIn("services", snap)
        self.assertIsNotNone(snap["rca"])
        self.assertEqual(snap["rca"]["root"], "inventory-service")
        self.assertIsNotNone(snap["plan"])
        e.apply_strategy(snap["plan"]["best"])
        self.assertTrue(any(ev["type"] == "action" for ev in e.timeline))


if __name__ == "__main__":
    unittest.main()
