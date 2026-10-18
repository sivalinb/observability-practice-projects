import concurrent.futures
import importlib.util
import json
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inference_project", ROOT / "projects/09-ai-performance-tracker/project.py")
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class InferenceTests(unittest.TestCase):
    def test_actual_stream_and_metrics(self):
        with project.serving(workers=1, scoring_rounds=2) as (endpoint, runtime):
            result = project.generate(endpoint, "the service", 5)
            self.assertEqual(result["tokens"], 5)
            self.assertGreater(result["client_ttft_seconds"], 0)
            metrics = urllib.request.urlopen(endpoint + "/metrics").read().decode()
            self.assertIn("inference_requests_total 1", metrics)
            self.assertIn("inference_generated_tokens_total 5", metrics)

    def test_worker_limit_under_concurrency(self):
        with project.serving(workers=1, scoring_rounds=5) as (endpoint, runtime):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: project.generate(endpoint, "the service", 4), range(4)))
            self.assertEqual(runtime.max_active, 1)
            self.assertEqual(len(results), 4)

    def test_excessive_token_budget_rejected(self):
        with project.serving(scoring_rounds=1) as (endpoint, runtime):
            with self.assertRaises(urllib.error.HTTPError) as context:
                project.generate(endpoint, "hello", 1000)
            self.assertEqual(context.exception.code, 400)

    def test_seed_repeats_token_sequence(self):
        with project.serving(scoring_rounds=1) as (endpoint, runtime):
            first = project.generate(endpoint, "the service", 8, 42)
            second = project.generate(endpoint, "the service", 8, 42)
            self.assertEqual(first["output"], second["output"])

    def test_nonobject_and_invalid_seed_rejected_before_streaming(self):
        with project.serving(scoring_rounds=1) as (endpoint, runtime):
            for body in ([], {"seed": "wrong"}, {"seed": True}, {"max_tokens": 1.5}):
                request = urllib.request.Request(endpoint + "/generate", json.dumps(body).encode(), {"Content-Type": "application/json"})
                with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(context.exception.code, 400)
            self.assertEqual(runtime.records, [])

    def test_ingress_limit_rejects_additional_connection(self):
        with project.serving(scoring_rounds=1, ingress_limit=1) as (endpoint, runtime):
            from urllib.parse import urlsplit
            address = urlsplit(endpoint)
            with socket.create_connection((address.hostname, address.port), timeout=3) as first:
                first.sendall(b"GET /metrics HTTP/1.1\r\nHost: local\r\n")
                try:
                    # Admission happens before HTTP parsing. Read the rejection
                    # directly: a client concurrently writing a POST body may
                    # instead observe EPIPE/ECONNRESET when its socket closes.
                    with socket.create_connection((address.hostname, address.port), timeout=3) as rejected:
                        response = rejected.recv(4096)
                        self.assertTrue(response.startswith(b"HTTP/1.1 503 Service Unavailable"), response)
                finally:
                    first.sendall(b"Connection: close\r\n\r\n")
                    while first.recv(4096):
                        pass


if __name__ == "__main__":
    unittest.main()
