from datetime import date
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "projects/02-metric-quality-checker"
SPEC = importlib.util.spec_from_file_location("contracts_project", HERE / "project.py")
PROJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROJECT)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads((HERE / "contract.json").read_text())
        self.snapshot = json.loads((HERE / "snapshot-after.json").read_text())

    def test_before_after_and_histogram_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = PROJECT.run_demo(Path(temporary))
            self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])
            self.assertEqual(report["measurements"]["histogram_series_before"], 168)
            self.assertEqual(report["measurements"]["histogram_series_after"], 14)

    def test_contract_upper_bound_detected_without_high_observation(self):
        self.contract["metrics"]["http_request_duration_seconds"]["max_series"] = 20
        result = PROJECT.validate(self.contract, self.snapshot, as_of=date(2026, 10, 18))
        self.assertIn("contract_cardinality", {issue["rule"] for issue in result["issues"]})
        self.assertNotIn("observed_cardinality", {issue["rule"] for issue in result["issues"]})

    def test_nan_and_negative_counter_fail(self):
        self.snapshot["metrics"][0]["samples"][0]["value"] = float("nan")
        self.snapshot["metrics"][1]["samples"][0]["value"] = -1
        result = PROJECT.validate(self.contract, self.snapshot)
        self.assertEqual({"sample_value", "negative_value"}, {issue["rule"] for issue in result["issues"]})

    def test_deprecation_boundary(self):
        snapshot = {"metrics": [{"name": "legacy_requests_total", "type": "counter", "unit": "requests", "owner": "checkout", "samples": [{"labels": {"service": "checkout"}, "value": 1}]}]}
        self.assertTrue(PROJECT.validate(self.contract, snapshot, as_of=date(2026, 9, 30))["accepted"])
        self.assertFalse(PROJECT.validate(self.contract, snapshot, as_of=date(2026, 10, 1))["accepted"])

    def test_duplicate_descriptor_cannot_hide_cardinality_budget(self):
        self.snapshot["metrics"].append(self.snapshot["metrics"][0])
        result = PROJECT.validate(self.contract, self.snapshot)
        self.assertIn("duplicate_descriptor", {issue["rule"] for issue in result["issues"]})


if __name__ == "__main__":
    unittest.main()
