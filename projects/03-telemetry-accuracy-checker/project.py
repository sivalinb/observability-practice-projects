"""An HTTP synthetic canary verifies the stored/queryable telemetry contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


EXPECTED_LABELS = {"service": "synthetic-checkout", "region": "local", "owner": "platform"}


class CanaryStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as database:
            database.execute("CREATE TABLE samples (stream TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(stream, sequence))")

    def connect(self):
        database = sqlite3.connect(self.path)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def ingest(self, stream: str, sample: dict, fault: str):
        # Faults are applied inside the collector, after the emitter creates its
        # independent expectations. They reproduce green-process/bad-data cases.
        sample = json.loads(json.dumps(sample))
        if fault == "dropped" and sample["sequence"] == 3:
            return
        if fault == "stale":
            sample["timestamp"] -= 120
        if fault == "wrong_label" and sample["sequence"] == 2:
            sample["labels"]["region"] = "incorrect-region"
        if fault == "wrong_value" and sample["sequence"] == 2:
            sample["value"] += 100
        with self.connect() as database:
            database.execute("INSERT OR REPLACE INTO samples VALUES (?, ?, ?)", (stream, sample["sequence"], json.dumps(sample)))

    def query(self, stream: str, fault: str) -> list[dict]:
        with self.connect() as database:
            if fault == "query_broken":
                # Real SQL error behind a healthy HTTP process.
                database.execute("SELECT body FROM renamed_table_without_migration")
            return [json.loads(row[0]) for row in database.execute("SELECT body FROM samples WHERE stream=? ORDER BY sequence", (stream,))]


def start_pipeline(store: CanaryStore, faults: dict[str, str]):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, value: dict):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            if self.path != "/ingest":
                return self.respond(404, {"error": "unknown endpoint"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                stream, sample = body["stream"], body["sample"]
                store.ingest(stream, sample, faults.get(stream, "healthy"))
            except (KeyError, ValueError, TypeError) as error:
                return self.respond(400, {"error": str(error)})
            self.respond(202, {"accepted": True})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self.respond(200, {"process": "healthy"})
            if parsed.path != "/query":
                return self.respond(404, {"error": "unknown endpoint"})
            stream = parse_qs(parsed.query).get("stream", [""])[0]
            if not stream:
                return self.respond(400, {"error": "stream required"})
            try:
                samples = store.query(stream, faults.get(stream, "healthy"))
            except sqlite3.OperationalError as error:
                return self.respond(503, {"error": str(error)})
            self.respond(200, {"samples": samples})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def request_json(url: str, body: dict | None = None) -> dict:
    request = Request(url, None if body is None else json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def inspect_samples(samples: list[dict], expected_sequences: list[int], now: float, max_age: float = 30) -> dict:
    reasons = []
    seen = [sample["sequence"] for sample in samples]
    expected_set, seen_set = set(expected_sequences), set(seen)
    missing = sorted(expected_set - seen_set)
    unexpected = sorted(seen_set - expected_set)
    if missing or unexpected or len(seen) != len(seen_set):
        reasons.append("sequence_gap")
    if not samples:
        reasons.append("dead_man")
    ages = [now - sample["timestamp"] for sample in samples]
    if ages and (max(ages) > max_age or min(ages) < -5):
        reasons.append("stale_data")
    wrong_labels = [sample["sequence"] for sample in samples if sample.get("labels") != EXPECTED_LABELS]
    if wrong_labels:
        reasons.append("wrong_label")
    wrong_values = [sample["sequence"] for sample in samples if sample.get("value") != sample["sequence"] * 10 + 7]
    if wrong_values:
        reasons.append("wrong_value")
    return {"healthy": not reasons, "reasons": reasons, "expected_count": len(expected_sequences), "observed_count": len(samples), "missing_sequences": missing, "unexpected_sequences": unexpected,
            "wrong_label_sequences": wrong_labels, "wrong_value_sequences": wrong_values, "oldest_age_seconds": round(max(ages), 3) if ages else None}


def inspect_query(url: str, expected_sequences: list[int], now: float) -> dict:
    try:
        samples = request_json(url)["samples"]
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        return {"healthy": False, "reasons": ["query_unavailable"], "error": str(error), "expected_count": len(expected_sequences), "observed_count": 0}
    return inspect_samples(samples, expected_sequences, now)


def query_prometheus(base_url: str, expression: str) -> dict:
    """Read a real Prometheus instant query for an optional deployment check.

    This reports transport/query status only. Strong sequence checks use the
    local canary's explicit event history, because a scrape can miss emissions.
    """
    result = request_json(base_url.rstrip("/") + "/api/v1/query?" + urlencode({"query": expression}))
    if result.get("status") != "success":
        raise ValueError(f"Prometheus query failed: {result}")
    return result


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = Path(tempfile.mkdtemp(prefix="accuracy-", dir=output_dir))
    store = CanaryStore(state_dir / "canary.sqlite")
    expected_reason = {"healthy": None, "dropped": "sequence_gap", "stale": "stale_data", "wrong_label": "wrong_label", "wrong_value": "wrong_value", "query_broken": "query_unavailable", "dead_man": "dead_man"}
    faults = dict(zip(expected_reason, expected_reason))
    server, thread = start_pipeline(store, faults)
    base = f"http://127.0.0.1:{server.server_port}"
    outcomes = {}
    start = time.perf_counter()
    transport_accepted = 0
    try:
        for scenario in expected_reason:
            process_healthy = request_json(base + "/health")["process"] == "healthy"
            sequences = list(range(1, 7))
            timestamp = time.time()
            if scenario != "dead_man":
                for sequence in sequences:
                    sample = {"sequence": sequence, "value": sequence * 10 + 7, "timestamp": timestamp, "labels": EXPECTED_LABELS}
                    accepted = request_json(base + "/ingest", {"stream": scenario, "sample": sample})
                    transport_accepted += int(accepted["accepted"])
            result = inspect_query(base + "/query?" + urlencode({"stream": scenario}), sequences, time.time())
            result["process_healthy"] = process_healthy
            outcomes[scenario] = result
        duration = time.perf_counter() - start
        checks = []
        for scenario, reason in expected_reason.items():
            outcome = outcomes[scenario]
            detected = outcome["healthy"] if reason is None else not outcome["healthy"] and reason in outcome["reasons"]
            checks.append({"name": "Healthy canary accepted" if reason is None else f"Detect {scenario.replace('_', ' ')}", "passed": detected, "detail": f"Process healthy={outcome['process_healthy']}; observed={outcome['observed_count']}/6; signals={','.join(outcome['reasons']) or 'none'}."})
        checks.append({"name": "Green health does not hide bad telemetry", "passed": all(value["process_healthy"] for value in outcomes.values()) and all(not value["healthy"] for key, value in outcomes.items() if key != "healthy"), "detail": "Every process health check returned 200 while all six independent data failures were caught."})
        (output_dir / "canary-outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n")
        with store.connect() as database:
            persisted = database.execute("SELECT count(*) FROM samples").fetchone()[0]
        return {"id": "03", "title": "Telemetry Accuracy Checker", "summary": "An independent synthetic canary detects sequence loss, stale data, label/value corruption, failed queries and silence through a real HTTP-to-SQLite query path.", "checks": checks,
                "measurements": {"scenarios": len(outcomes), "faults_detected": sum(not value["healthy"] for value in outcomes.values()), "http_ingest_accepts": transport_accepted, "persisted_samples": persisted, "healthy_canary_samples": outcomes["healthy"]["observed_count"], "dropped_missing_sequence": str(outcomes["dropped"]["missing_sequences"]), "stale_oldest_seconds": outcomes["stale"]["oldest_age_seconds"], "suite_seconds": round(duration, 4)},
                "timeline": [{"phase": "Independent expectation", "detail": "The emitter knows six sequence IDs, exact arithmetic values and bounded labels before collection."}, {"phase": "Actual collection", "detail": f"{transport_accepted} HTTP accepts traverse a persistent SQLite collection path; one intentionally drops before storage."}, {"phase": "Fault injection", "detail": "Collection rewrites data, loses one sample, or breaks SQL while process health remains green."}, {"phase": "Query verification", "detail": "The reader compares stored data with expectations and checks freshness; silence triggers dead-man detection."}],
                "scenarios": ["healthy end-to-end canary", "collector drops acknowledged sample", "stale stored timestamps", "incorrect resource label", "corrupted canary value", "query layer SQL failure", "absent producer / dead-man", "green process health during data failures"],
                "artifacts": {"store": str(store.path), "outcomes": str(output_dir / "canary-outcomes.json")}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--query", default="up")
    arguments = parser.parse_args()
    print(json.dumps(query_prometheus(arguments.prometheus_url, arguments.query), indent=2))
