import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cost_project", ROOT / "projects/07-telemetry-cost-optimizer/project.py")
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class CostTests(unittest.TestCase):
    def test_diagnostic_guardrails(self):
        with tempfile.TemporaryDirectory() as directory:
            report = project.run_demo(Path(directory))
            self.assertTrue(all(check["passed"] for check in report["checks"]))
            self.assertLess(report["measurements"]["aggregate_gzip_bytes"], report["measurements"]["baseline_gzip_bytes"])

    def test_histograms_are_cumulative_and_mergeable(self):
        first = project.histogram([10, 100, 900])
        second = project.histogram([50, 1200])
        self.assertEqual({key: first[key] + second[key] for key in first}, project.histogram([10, 100, 900, 50, 1200]))
        self.assertEqual(first["inf"], 3)

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            project.apply_policy([], "invented")

    def test_sampled_events_are_not_valid_rate_denominators(self):
        source = project.fixture()
        kept = project.apply_policy(source, "incident-aware")
        original_rate = sum(row["status"] >= 500 for row in source) / len(source)
        biased_rate = sum(row["status"] >= 500 for row in kept) / len(kept)
        self.assertGreater(biased_rate, original_rate * 2)


if __name__ == "__main__":
    unittest.main()
