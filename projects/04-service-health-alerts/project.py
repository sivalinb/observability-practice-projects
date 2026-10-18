"""Run an actual HTTP batch workload and evaluate timestamped SLO events."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Sample:
    timestamp: float
    total: int
    bad: int

    def __post_init__(self):
        if self.total < 0 or not 0 <= self.bad <= self.total:
            raise ValueError("bad must be between zero and total")


def evaluate(samples: list[Sample], now: float, slo: float = .999,
             freshness_seconds: float = 120) -> dict:
    """Event-time evaluation. Late/future samples cannot make stale data healthy."""
    if not 0 < slo < 1:
        raise ValueError("SLO must be strictly between zero and one")
    available = [p for p in samples if p.timestamp <= now]
    latest = max((p.timestamp for p in available), default=None)
    fresh = latest is not None and now - latest <= freshness_seconds
    windows = {}
    for name, seconds in (("5m", 300), ("30m", 1800), ("1h", 3600), ("6h", 21600)):
        points = [p for p in available if now - seconds < p.timestamp <= now]
        total, bad = sum(p.total for p in points), sum(p.bad for p in points)
        windows[name] = {"total": total, "bad": bad,
                         "burn_rate": bad / total / (1 - slo) if total else None}
    def above(window, threshold):
        burn = windows[window]["burn_rate"]
        return burn is not None and burn > threshold
    fast = fresh and above("1h", 14.4) and above("5m", 14.4)
    slow = fresh and above("6h", 6) and above("30m", 6)
    if not fresh:
        state = "missing_data"
    elif windows["5m"]["total"] == 0:
        state = "no_traffic"
    elif fast:
        state = "page_fast"
    elif slow:
        state = "page_slow"
    else:
        state = "healthy"
    return {"state": state, "fast_firing": fast, "slow_firing": slow,
            "fresh": fresh, "windows": windows, "latest_sample": latest}


def execute_batch(total: int, bad: int) -> tuple[int, int, int]:
    """Each iteration executes a real operation; failures are deterministic fixtures."""
    successes = failures = checksum = 0
    for i in range(total):
        try:
            if i < bad:
                raise RuntimeError("injected operation failure")
            checksum = (checksum + i * 17) % 65521
            successes += 1
        except RuntimeError:
            failures += 1
    return successes + failures, failures, checksum


class WorkloadHandler(BaseHTTPRequestHandler):
    http_calls = 0
    operations = 0
    counter_lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048:
                raise ValueError("invalid payload length")
            request = json.loads(self.rfile.read(length))
            total, bad = int(request["total"]), int(request["bad"])
            if not 0 < total <= 10000 or not 0 <= bad <= total:
                raise ValueError("invalid batch")
            measured_total, measured_bad, checksum = execute_batch(total, bad)
            with self.counter_lock:
                type(self).http_calls += 1
                type(self).operations += measured_total
            response = json.dumps({"total": measured_total, "bad": measured_bad,
                                   "checksum": checksum}).encode()
            self.send_response(200)
        except (ValueError, KeyError, json.JSONDecodeError):
            response = b'{"error":"invalid workload request"}'
            self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    WorkloadHandler.http_calls = WorkloadHandler.operations = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), WorkloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    now = 21600.0
    results = {}
    series = {}
    try:
        for scenario in ("brief_spike", "sustained_failure", "slow_burn", "recovery"):
            points = []
            for minute in range(1, 361):
                if scenario == "brief_spike":
                    bad = 200 if minute == 360 else 0
                elif scenario == "sustained_failure":
                    bad = 200 if minute > 330 else 0
                elif scenario == "slow_burn":
                    bad = 8
                else:
                    bad = 200 if 250 < minute <= 320 else 0
                payload = json.dumps({"total": 1000, "bad": bad}).encode()
                request = Request(f"http://127.0.0.1:{server.server_port}/batch", data=payload,
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    measured = json.load(response)
                points.append(Sample(minute * 60, measured["total"], measured["bad"]))
            series[scenario] = points
            results[scenario] = evaluate(points, now)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    results["no_traffic"] = evaluate([Sample(now, 0, 0)], now)
    results["absent_data"] = evaluate([], now)
    results["late_data"] = evaluate([Sample(now - 600, 1000, 0)], now)
    expected = {"brief_spike": "healthy", "sustained_failure": "page_fast",
                "slow_burn": "page_slow", "recovery": "healthy",
                "no_traffic": "no_traffic", "absent_data": "missing_data",
                "late_data": "missing_data"}
    checks = [{"name": name, "passed": results[name]["state"] == state,
               "detail": f"expected {state}; observed {results[name]['state']}"}
              for name, state in expected.items()]
    checks.append({"name": "real_http_workload", "passed": WorkloadHandler.http_calls == 1440
                   and WorkloadHandler.operations == 1440000,
                   "detail": f"{WorkloadHandler.http_calls} HTTP batches, {WorkloadHandler.operations} executed operations"})
    (output_dir / "window-evaluations.json").write_text(json.dumps(results, indent=2))
    (output_dir / "events.json").write_text(json.dumps({name: [asdict(p) for p in points]
                                                        for name, points in series.items()}))
    return {"id": "04", "title": "Service Health Alerts",
            "summary": "Multiwindow burn-rate alerts distinguish brief spikes, sustained failures, slow burns, recovery, and missing data.",
            "checks": checks, "measurements": {"http_batches": WorkloadHandler.http_calls,
              "executed_operations": WorkloadHandler.operations, "slo_percent": 99.9,
              "fast_burn_rate": round(results["sustained_failure"]["windows"]["1h"]["burn_rate"], 2),
              "slow_burn_rate": round(results["slow_burn"]["windows"]["6h"]["burn_rate"], 2),
              "fast_page_budget_percent": 2.0, "slow_page_budget_percent": 5.0},
            "timeline": [{"phase": "Generate", "detail": "Run 1,440 loopback HTTP batches; execute 1.44 million deterministic operations."},
                         {"phase": "Evaluate", "detail": "Apply 5m/1h fast and 30m/6h slow burn-rate pairs on controlled event time."},
                         {"phase": "Decide", "detail": "Write evaluated windows and preserve missing-data/no-traffic states."}],
            "scenarios": list(expected)}
