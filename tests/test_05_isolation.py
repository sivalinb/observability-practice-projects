import importlib.util
import pathlib
import sys
import threading
import unittest

PATH = pathlib.Path(__file__).resolve().parents[1] / "projects/05-shared-platform-protection/project.py"
SPEC = importlib.util.spec_from_file_location("isolation05", PATH)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = mod.Gateway({"alpha": mod.Policy(capacity=2), "beta": mod.Policy(capacity=10)})
        self.server = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.handler_for(self.gateway))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.gateway.close()

    def request(self, token, **kwargs):
        return mod.request_query(self.server.server_port, token, **kwargs)

    def test_unauthorized(self):
        self.assertEqual(self.request("unknown")[0], 401)

    def test_forged_tenant_header_does_not_change_identity(self):
        status, body = self.request("demo-alpha-token", forged_tenant="beta")
        self.assertEqual((status, body["tenant"]), (200, "alpha"))

    def test_rate_quota_does_not_limit_other_tenant(self):
        self.assertEqual(self.request("demo-alpha-token")[0], 200)
        self.assertEqual(self.request("demo-alpha-token")[0], 200)
        self.assertEqual(self.request("demo-alpha-token")[0], 429)
        self.assertEqual(self.request("demo-beta-token")[0], 200)

    def test_oversized_query(self):
        self.assertEqual(self.request("demo-beta-token", query="z" * 513)[0], 413)

    def test_work_cost_bound(self):
        self.assertEqual(self.request("demo-beta-token", work_ms=201)[0], 400)


class SchedulerTests(unittest.TestCase):
    def test_weighted_fairness_under_backlog(self):
        scheduler = mod.WeightedScheduler({"a": mod.Policy(weight=2), "b": mod.Policy(weight=1)})
        for tenant in ("a", "b"):
            for _ in range(15):
                self.assertTrue(scheduler.put(mod.Job(tenant)))
        seen = []
        for _ in range(15):
            job = scheduler.get()
            seen.append(job.tenant)
            scheduler.complete(job)
        self.assertEqual((seen.count("a"), seen.count("b")), (10, 5))

    def test_concurrency_and_queue_bounds(self):
        scheduler = mod.WeightedScheduler({"a": mod.Policy(concurrency=1, queue_capacity=1), "b": mod.Policy()})
        self.assertTrue(scheduler.put(mod.Job("a")))
        self.assertFalse(scheduler.put(mod.Job("a")))
        first = scheduler.get()
        self.assertTrue(scheduler.put(mod.Job("a")))
        self.assertTrue(scheduler.put(mod.Job("b")))
        other = scheduler.get()
        self.assertEqual(other.tenant, "b")
        self.assertIsNone(scheduler.get(timeout=.001))
        scheduler.complete(first)
        self.assertEqual(scheduler.get().tenant, "a")


if __name__ == "__main__":
    unittest.main()
