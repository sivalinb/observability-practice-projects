import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.kafka_bridge import DeliveryJournal
from shared.server import render
from shared import runner


class SharedEvidenceTests(unittest.TestCase):
    def test_pinned_kafka_offset_includes_leader_epoch(self):
        from kafka import OffsetAndMetadata
        offset = OffsetAndMetadata(4, "", -1)
        self.assertEqual(offset.offset, 4)
        self.assertEqual(offset.leader_epoch, -1)

    def test_delivery_journal_survives_connection_restart_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "state.db")
            journal = DeliveryJournal(path)
            self.assertEqual(journal.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            journal.mark("event-1", "events", 0, 3)
            journal.db.close()
            recovered = DeliveryJournal(path)
            self.assertTrue(recovered.contains("event-1"))
            recovered.mark("event-1", "events", 0, 3)
            self.assertEqual(recovered.db.execute("SELECT COUNT(*) FROM delivered").fetchone()[0], 1)
            recovered.db.close()

    def test_evidence_view_escapes_untrusted_text(self):
        report = {"id": "01", "title": "<script>bad()</script>", "checks": [{"name": "x", "passed": False, "detail": "<img onerror=x>"}], "measurements": {}, "timeline": []}
        html = render(report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("0 / 1", html)

    def test_failed_rerun_replaces_previous_green_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "artifacts/01").mkdir(parents=True)
            (root / "artifacts/01/latest.json").write_text('{"checks":[{"passed":true}]}')
            def fail(_):
                raise RuntimeError("injected failure")
            with patch.object(runner, "ARTIFACTS", root / "artifacts"), patch.object(runner, "LOG_DIR", root / "logs"), patch.object(runner, "load_project", return_value=SimpleNamespace(run_demo=fail)):
                result = runner.execute("01")
            persisted = json.loads((root / "artifacts/01/latest.json").read_text())
            self.assertFalse(result["checks"][0]["passed"])
            self.assertFalse(persisted["checks"][0]["passed"])
            self.assertIn("injected failure", persisted["checks"][0]["detail"])
