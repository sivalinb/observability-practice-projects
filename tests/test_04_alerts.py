import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[1] / "projects/04-service-health-alerts/project.py"
SPEC = importlib.util.spec_from_file_location("alerts04", PATH)
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)
LIVE_SPEC = importlib.util.spec_from_file_location("live04", PATH.parent / "live.py")
live = importlib.util.module_from_spec(LIVE_SPEC)
with patch.dict(sys.modules, {"project": mod}):
    LIVE_SPEC.loader.exec_module(live)


class AlertTests(unittest.TestCase):
    def samples(self, failures):
        return [mod.Sample(m * 60, 1000, failures(m)) for m in range(1, 361)]

    def test_short_spike_is_not_page(self):
        result = mod.evaluate(self.samples(lambda m: 200 if m == 360 else 0), 21600)
        self.assertEqual(result["state"], "healthy")
        self.assertGreater(result["windows"]["5m"]["burn_rate"], 14.4)

    def test_sustained_failure_pages(self):
        self.assertEqual(mod.evaluate(self.samples(lambda m: 200 if m > 330 else 0), 21600)["state"], "page_fast")

    def test_slow_burn_pages(self):
        self.assertEqual(mod.evaluate(self.samples(lambda _: 8), 21600)["state"], "page_slow")

    def test_recovery_stops_page_despite_long_window(self):
        self.assertEqual(mod.evaluate(self.samples(lambda m: 200 if 250 < m <= 320 else 0), 21600)["state"], "healthy")

    def test_no_traffic_absence_and_late_data_are_distinct(self):
        self.assertEqual(mod.evaluate([mod.Sample(21600, 0, 0)], 21600)["state"], "no_traffic")
        self.assertEqual(mod.evaluate([], 21600)["state"], "missing_data")
        self.assertEqual(mod.evaluate([mod.Sample(21000, 1000, 0)], 21600)["state"], "missing_data")

    def test_future_sample_does_not_hide_missing_data(self):
        self.assertEqual(mod.evaluate([mod.Sample(21601, 1000, 0)], 21600)["state"], "missing_data")

    def test_invalid_counts_rejected(self):
        with self.assertRaises(ValueError):
            mod.Sample(0, 2, 3)

    def test_operation_counters_measure_executed_work(self):
        self.assertEqual(mod.execute_batch(1000, 8)[:2], (1000, 8))

    def test_live_source_rejects_invalid_initial_counts_before_binding(self):
        for bad in (-1, 1001, .5, "20"):
            with self.assertRaises(ValueError):
                live.serve(port=0, initial_bad=bad)

    def test_live_metric_exposition_preserves_actual_counts(self):
        text = live.metrics_text({"total": 2000, "bad": 16, "last_sample": 1234.5})
        self.assertIn('prep_sli_requests_total{project="04"} 2000\n', text)
        self.assertIn('prep_sli_errors_total{project="04"} 16\n', text)
        self.assertIn('prep_sli_last_sample_timestamp_seconds{project="04"} 1234.5\n', text)


if __name__ == "__main__":
    unittest.main()
