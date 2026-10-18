import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("accuracy_project", ROOT / "projects/03-telemetry-accuracy-checker/project.py")
PROJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROJECT)


class AccuracyTests(unittest.TestCase):
    def test_faults_traverse_http_and_query_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = PROJECT.run_demo(Path(temporary))
            self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])
            self.assertEqual(report["measurements"]["faults_detected"], 6)
            self.assertEqual(report["measurements"]["http_ingest_accepts"], 36)
            self.assertEqual(report["measurements"]["persisted_samples"], 35)

    def test_future_timestamps_and_duplicate_sequence_are_unhealthy(self):
        sample = {"sequence": 1, "timestamp": 200, "labels": PROJECT.EXPECTED_LABELS, "value": 17}
        result = PROJECT.inspect_samples([sample, sample], [1], now=100)
        self.assertIn("stale_data", result["reasons"])
        self.assertIn("sequence_gap", result["reasons"])

    def test_dead_man_cannot_pass_on_empty_input(self):
        result = PROJECT.inspect_samples([], [], now=100)
        self.assertFalse(result["healthy"])
        self.assertIn("dead_man", result["reasons"])

    def test_exact_value_and_label_contract(self):
        sample = {"sequence": 1, "timestamp": 99, "labels": PROJECT.EXPECTED_LABELS, "value": 17}
        self.assertTrue(PROJECT.inspect_samples([sample], [1], now=100)["healthy"])
        sample["labels"] = {"service": "wrong"}
        sample["value"] = 18
        result = PROJECT.inspect_samples([sample], [1], now=100)
        self.assertEqual({"wrong_label", "wrong_value"}, set(result["reasons"]))


if __name__ == "__main__":
    unittest.main()
