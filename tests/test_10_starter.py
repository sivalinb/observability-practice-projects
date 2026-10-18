import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("starter_project", ROOT / "projects/10-service-monitoring-starter-kit/project.py")
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads((ROOT / "projects/10-service-monitoring-starter-kit/service-contract.json").read_text())

    def test_generation_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(project.generate(self.contract, first), project.generate(self.contract, second))
            compile((Path(first) / "app.py").read_text(), "app.py", "exec")

    def test_owner_and_cardinality_validation(self):
        for change in ({"owner": ""}, {"metric_labels": ["request_id"]}, {"routes": ["/health", "/users/{id}"]}, {"availability_target": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                project.validate_contract({**self.contract, **change})

    def test_invalid_input_creates_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad"
            with self.assertRaises(ValueError):
                project.generate({**self.contract, "owner": ""}, target)
            self.assertFalse(target.exists())

    def test_end_to_end_generated_service(self):
        with tempfile.TemporaryDirectory() as directory:
            result = project.run_demo(Path(directory))
            self.assertTrue(all(row["passed"] for row in result["checks"]), result["checks"])

    def test_histogram_extends_beyond_large_latency_target(self):
        with tempfile.TemporaryDirectory() as directory:
            project.generate({**self.contract, "latency_target_seconds": 60}, directory)
            command = "import app; app.duration.labels(app.SERVICE, '/work', 'GET', '200').observe(70); print(app.generate_latest(app.registry).decode()); app.provider.shutdown()"
            result = subprocess.run([sys.executable, "-c", command], cwd=directory, text=True, capture_output=True, check=True)
            self.assertRegex(result.stdout, r'le="60\.0"[^\n]* 0\.0')
            self.assertRegex(result.stdout, r'le="120\.0"[^\n]* 1\.0')
            self.assertRegex(result.stdout, r'le="240\.0"[^\n]* 1\.0')


if __name__ == "__main__":
    unittest.main()
