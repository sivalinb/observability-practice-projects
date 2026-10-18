import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("recovery_project", ROOT / "projects/01-telemetry-recovery/project.py")
PROJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROJECT)


class RecoveryTests(unittest.TestCase):
    def test_http_outage_process_crash_and_dedupe(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = PROJECT.run_demo(Path(temporary))
            self.assertTrue(all(check["passed"] for check in report["checks"]), report["checks"])
            self.assertEqual(report["measurements"]["sink_unique_events"], 25)
            self.assertEqual(report["measurements"]["delivery_attempts"], 26)

    def test_checkpoint_refuses_skip_and_recovers_remaining_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.jsonl"
            wal = PROJECT.DurableWAL(path)
            wal.append({"event_id": "a"})
            wal.append({"event_id": "b"})
            with self.assertRaises(ValueError):
                wal.acknowledge(2)
            wal.acknowledge(1)
            self.assertEqual(list(PROJECT.DurableWAL(path).pending()), [(2, {"event_id": "b"})])

    def test_bad_complete_record_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "data.jsonl"
            path.write_text(json.dumps({"payload": {"event_id": "a"}, "checksum": "wrong"}) + "\n")
            before = path.read_bytes()
            with self.assertRaises(PROJECT.WALCorruption):
                PROJECT.DurableWAL(path)
            self.assertEqual(path.read_bytes(), before)

    def test_bounded_wal_rejects_without_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            wal = PROJECT.DurableWAL(Path(temporary) / "data.jsonl", max_bytes=1)
            with self.assertRaises(PROJECT.CapacityExceeded):
                wal.append({"event_id": "a"})
            self.assertEqual(wal.path.stat().st_size, 0)
            self.assertEqual(wal.records, [])


if __name__ == "__main__":
    unittest.main()
