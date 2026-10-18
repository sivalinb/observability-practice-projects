import importlib.util
import pathlib
import sys
import unittest

PATH = pathlib.Path(__file__).resolve().parents[1] / "projects/06-performance-troubleshooter/project.py"
SPEC = importlib.util.spec_from_file_location("performance06", PATH)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class PerformanceTests(unittest.TestCase):
    def test_w3c_context_parent_and_child(self):
        recorder = mod.SpanRecorder()
        trace_id = "1" * 32
        parent_id = "2" * 16
        with recorder.span("frontend", {"traceparent": f"00-{trace_id}-{parent_id}-01"}) as root:
            carrier = recorder.inject()
            with recorder.span("dependency", carrier) as child:
                self.assertEqual(child["trace_id"], trace_id)
                self.assertEqual(child["parent_id"], root["span_id"])
            self.assertEqual(root["parent_id"], parent_id)
        self.assertEqual(len(recorder.rows), 2)
        recorder.close()

    def test_malformed_traceparent_rejected(self):
        self.assertIsNone(mod.parse_traceparent({"traceparent": "bad"}))
        self.assertIsNone(mod.parse_traceparent({"traceparent": "00-" + "0" * 32 + "-" + "1" * 16 + "-01"}))

    def test_head_and_tail_have_different_information(self):
        rows = [{"trace_id": f"{n:032x}", "name": "frontend.request", "duration_ms": duration,
                 "attributes": {"error": error}}
                for n, duration, error in ((1, 2, False), (2, 50, False), (3, 3, True), (4, 1, False))]
        policies = mod.retain_completed_traces(rows)
        self.assertEqual(set(policies["head"]), {f"{4:032x}"})
        self.assertEqual(set(policies["tail"]), {f"{2:032x}", f"{3:032x}"})

    def test_tail_buffer_capacity_is_explicit(self):
        rows = [{"trace_id": str(n), "name": "frontend.request", "duration_ms": 1, "attributes": {}} for n in (1, 2)]
        with self.assertRaises(BufferError):
            mod.retain_completed_traces(rows, max_traces=1)

    def test_cpu_work_deterministic(self):
        self.assertEqual(mod.cpu_hotspot(100), mod.cpu_hotspot(100))
        self.assertNotEqual(mod.cpu_hotspot(100), mod.cpu_hotspot(101))


if __name__ == "__main__":
    unittest.main()
