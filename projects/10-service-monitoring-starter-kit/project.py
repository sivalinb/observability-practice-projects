"""Generate, launch and verify a complete instrumented Python service."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ID = "10"
TITLE = "Service Monitoring Starter Kit"

APP_SOURCE = r'''"""Generated HTTP service: bounded metrics, real OpenTelemetry spans, JSON logs."""
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

CONTRACT = json.loads(Path(__file__).with_name("service-contract.json").read_text())
SERVICE = CONTRACT["service_name"]
logging.basicConfig(format="%(message)s", level=logging.INFO)

class JsonSpanExporter(SpanExporter):
    def export(self, spans):
        path = Path(os.environ.get("SERVICE_TRACE_FILE", "traces.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            for span in spans:
                handle.write(json.dumps({"name": span.name, "trace_id": format(span.context.trace_id, "032x"),
                    "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                    "status": span.status.status_code.name, "attributes": dict(span.attributes),
                    "duration_ns": span.end_time - span.start_time}) + "\n")
        return SpanExportResult.SUCCESS
    def shutdown(self):
        pass

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE, "service.owner": CONTRACT["owner"]}))
provider.add_span_processor(SimpleSpanProcessor(JsonSpanExporter()))
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(SERVICE)
registry = CollectorRegistry()
LABELS = ("service", "route", "method", "status_code")
requests = Counter("service_http_requests_total", "Completed requests", LABELS, registry=registry)
duration = Histogram("service_http_request_duration_seconds", "Request duration", LABELS,
                     buckets=sorted(set((.005, .01, .025, .05, .1, .25, .5, 1, 2.5,
                         CONTRACT["latency_target_seconds"], CONTRACT["latency_target_seconds"] * 2,
                         CONTRACT["latency_target_seconds"] * 4))), registry=registry)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/metrics":
            data = generate_latest(registry)
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        started = time.perf_counter()
        route = parts.path if parts.path in CONTRACT["routes"] else "__unmatched__"
        code = 404 if route == "__unmatched__" else 200
        if route == "/work" and parse_qs(parts.query).get("fail") == ["1"]:
            code = 500
        carrier = {key.lower(): value for key, value in self.headers.items()}
        with tracer.start_as_current_span("GET " + route, context=extract(carrier), kind=SpanKind.SERVER,
            attributes={"http.request.method": "GET", "http.route": route, "http.response.status_code": code}) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            if code >= 500:
                span.set_status(Status(StatusCode.ERROR, "injected work failure"))
            if route == "/work":
                with tracer.start_as_current_span("calculate-checksum"):
                    value = sum(index * index for index in range(1000))
            data = json.dumps({"service": SERVICE, "owner": CONTRACT["owner"], "status": code,
                               "trace_id": trace_id, "route": route}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Trace-Id", trace_id)
            self.end_headers()
            self.wfile.write(data)
            requests.labels(SERVICE, route, "GET", str(code)).inc()
            duration.labels(SERVICE, route, "GET", str(code)).observe(time.perf_counter() - started)
            logging.info(json.dumps({"event": "request_completed", "service": SERVICE, "owner": CONTRACT["owner"],
                         "route": route, "status_code": code, "trace_id": trace_id}))

if __name__ == "__main__":
    server = ThreadingHTTPServer((os.environ.get("SERVICE_HOST", "127.0.0.1"), int(os.environ.get("SERVICE_PORT", "8080"))), Handler)
    ready = os.environ.get("SERVICE_READY_PATH")
    if ready:
        Path(ready).write_text(json.dumps({"port": server.server_port}))
    try:
        server.serve_forever()
    finally:
        provider.shutdown()
        server.server_close()
'''


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError("Contract must be a JSON object")
    required = {"service_name", "owner", "contact", "routes", "metric_labels", "availability_target", "latency_target_seconds"}
    if set(contract) != required:
        raise ValueError("Contract must contain exactly " + ", ".join(sorted(required)))
    for key in ("service_name", "owner"):
        if not isinstance(contract[key], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", contract[key]):
            raise ValueError(key + " must use 2..40 lowercase letters, digits or hyphens")
    if not isinstance(contract["contact"], str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contract["contact"]):
        raise ValueError("contact must be an owner email address")
    routes = contract["routes"]
    if not isinstance(routes, list) or not 1 <= len(routes) <= 10 or any(not isinstance(route, str) for route in routes) or len(routes) != len(set(routes)):
        raise ValueError("routes must contain 1..10 unique bounded routes")
    if any(not isinstance(route, str) or not re.fullmatch(r"/[a-z0-9/-]{1,60}", route) or route == "/metrics" for route in routes):
        raise ValueError("routes must be static lowercase paths; /metrics is reserved")
    if "/health" not in routes:
        raise ValueError("routes must include /health")
    if contract["metric_labels"] != ["service", "route", "method", "status_code"]:
        raise ValueError("metric_labels must be the bounded service/route/method/status_code schema")
    availability = contract["availability_target"]
    latency = contract["latency_target_seconds"]
    if isinstance(availability, bool) or not isinstance(availability, (int, float)) or not .9 <= availability < 1:
        raise ValueError("availability_target must be >=0.9 and <1")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not 0 < latency <= 60:
        raise ValueError("latency_target_seconds must be >0 and <=60")
    return contract


def generate(contract, destination):
    validate_contract(contract)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    service = contract["service_name"]
    selector = '{service="' + service + '"}'
    errors = 'sum(rate(service_http_requests_total{service="' + service + '",status_code=~"5.."}[5m]))'
    total = 'sum(rate(service_http_requests_total' + selector + '[5m]))'
    dashboard = {"title": service + " service health", "uid": service, "schemaVersion": 39, "version": 1,
                 "tags": ["generated", contract["owner"]], "time": {"from": "now-15m", "to": "now"},
                 "panels": [
                    {"id": 1, "type": "timeseries", "title": "Request rate", "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                     "datasource": {"type": "prometheus", "uid": "prometheus"}, "targets": [{"refId": "A", "expr": total}]},
                    {"id": 2, "type": "timeseries", "title": "Server error ratio", "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                     "datasource": {"type": "prometheus", "uid": "prometheus"}, "targets": [{"refId": "A", "expr": errors + " / clamp_min(" + total + ", 0.001)"}]},
                    {"id": 3, "type": "timeseries", "title": "p95 request duration", "gridPos": {"x": 0, "y": 8, "w": 24, "h": 8},
                     "datasource": {"type": "prometheus", "uid": "prometheus"}, "targets": [{"refId": "A", "expr": 'histogram_quantile(0.95, sum by (le) (rate(service_http_request_duration_seconds_bucket' + selector + '[5m])))'}]},
                 ]}
    error_budget = 1 - contract["availability_target"]
    alerts = "\n".join([
        "groups:", "  - name: " + service, "    rules:", "      - alert: ServiceFastBurn",
        '        expr: (' + errors + ' / clamp_min(' + total + ', 0.001)) > ' + str(round(14.4 * error_budget, 8)),
        "        for: 2m", "        labels:", "          severity: page", "          owner: " + contract["owner"],
        "        annotations:", '          summary: "' + service + ' is consuming its availability budget quickly"',
        "      - alert: ServiceLatencyHigh",
        "        expr: histogram_quantile(0.95, sum by (le) (rate(service_http_request_duration_seconds_bucket" + selector + "[5m]))) > " + str(contract["latency_target_seconds"]),
        "        for: 5m", "        labels:", "          severity: ticket", "          owner: " + contract["owner"], "",
    ])
    files = {
        "app.py": APP_SOURCE,
        "service-contract.json": json.dumps(contract, indent=2, sort_keys=True) + "\n",
        "requirements.txt": "opentelemetry-sdk==1.38.0\nopentelemetry-exporter-otlp-proto-http==1.38.0\nprometheus-client==0.23.1\n",
        "Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY app.py service-contract.json ./\nENV SERVICE_HOST=0.0.0.0 SERVICE_PORT=8080\nEXPOSE 8080\nCMD ["python", "app.py"]\n',
        "grafana-dashboard.json": json.dumps(dashboard, indent=2) + "\n",
        "prometheus-alerts.yaml": alerts,
        "prometheus-scrape.yaml": 'scrape_configs:\n  - job_name: ' + service + '\n    static_configs:\n      - targets: ["127.0.0.1:8080"]\n',
        "fluent-bit.conf": '[SERVICE]\n    Flush 1\n    Parsers_File /fluent-bit/etc/parsers.conf\n    storage.path /var/lib/fluent-bit/storage\n    storage.sync full\n    storage.checksum on\n[INPUT]\n    Name tail\n    Path /var/log/service/*.jsonl\n    Tag service.logs\n    Parser json\n    DB /var/lib/fluent-bit/tail.db\n    DB.journal_mode WAL\n    DB.Sync Full\n    Read_from_Head true\n    storage.type filesystem\n[OUTPUT]\n    Name loki\n    Match service.*\n    Host loki\n    Port 3100\n    Labels job=generated-service,service=' + service + '\n    Line_Format json\n    Retry_Limit False\n    storage.total_limit_size 64M\n',
        "parsers.conf": '[PARSER]\n    Name json\n    Format json\n',
        "README.md": "# " + service + "\n\nOwner: " + contract["owner"] + " (" + contract["contact"] + ")\n\n"
            "```sh\npython -m venv .venv\n. .venv/bin/activate\npip install -r requirements.txt\npython app.py 2>service.jsonl\n"
            "curl -s http://127.0.0.1:8080/health\ncurl -s http://127.0.0.1:8080/work\ncurl -s http://127.0.0.1:8080/metrics\n```\n\n"
            "Set OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 to export spans to an OTLP HTTP collector. "
            "JSON spans also persist to SERVICE_TRACE_FILE (default traces.jsonl). Logs carry trace IDs. "
            "Mount service.jsonl under /var/log/service and generated fluent-bit.conf/parsers.conf under /fluent-bit/etc for Fluent Bit. "
            "The Loki output targets loki:3100 on the repository Compose network. Persist both tail.db and the storage directory. "
            "Tail SQLite WAL preserves cursor transactions; filesystem chunks protect queued log payloads. "
            "The output has a finite 64 MB storage cap and can discard older queued chunks when the cap is reached. "
            "Import grafana-dashboard.json and add prometheus-alerts.yaml to Prometheus rule_files. "
            "The included fast-burn alert is a simple starter threshold; production paging should combine long and short windows. "
            "Unknown request paths become __unmatched__; request IDs never enter metric labels.\n",
    }
    for name, content in files.items():
        (destination / name).write_text(content)
    manifest = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in sorted(files.items())}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def run_demo(output_dir: Path) -> dict:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    contract = json.loads(Path(__file__).with_name("service-contract.json").read_text())
    generated = output_dir / "generated-service"
    manifest = generate(contract, generated)
    with tempfile.TemporaryDirectory() as second_dir:
        reproducible = manifest == generate(contract, Path(second_dir))
    invalid_cases = [({**contract, "owner": ""}, "missing owner"),
                     ({**contract, "metric_labels": contract["metric_labels"] + ["request_id"]}, "unbounded request label"),
                     ({**contract, "routes": ["/health", "/user/{id}"]}, "dynamic path"),
                     ({**contract, "availability_target": 1}, "impossible target")]
    rejected = []
    for candidate, name in invalid_cases:
        try:
            validate_contract(candidate)
        except ValueError:
            rejected.append(name)
    ready = output_dir / "service-ready.json"
    traces = output_dir / "service-traces.jsonl"
    ready.unlink(missing_ok=True)
    traces.unlink(missing_ok=True)
    env = {**os.environ, "SERVICE_PORT": "0", "SERVICE_HOST": "127.0.0.1", "SERVICE_READY_PATH": str(ready), "SERVICE_TRACE_FILE": str(traces)}
    # Local verification must not accidentally inherit a remote collector target.
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    log_path = output_dir / "service.jsonl"
    with log_path.open("w") as log_handle:
        process = subprocess.Popen([sys.executable, "app.py"], cwd=generated, env=env, stdout=log_handle, stderr=log_handle)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if process.poll() is not None:
                    raise RuntimeError("Generated service failed to start: " + log_path.read_text()[-2000:])
                if time.monotonic() > deadline:
                    raise RuntimeError("Generated service readiness timeout")
                time.sleep(.03)
            endpoint = "http://127.0.0.1:" + str(json.loads(ready.read_text())["port"])
            health_status, health_body = fetch(endpoint + "/health")
            expected_trace = "0123456789abcdef0123456789abcdef"
            work_status, work_body = fetch(endpoint + "/work", {"traceparent": "00-" + expected_trace + "-0123456789abcdef-01"})
            error_status, error_body = fetch(endpoint + "/work?fail=1")
            unknown_status, unknown_body = fetch(endpoint + "/user/very-specific-customer-123")
            metrics_status, metrics = fetch(endpoint + "/metrics")
            # Wait until the exporter finishes this request's synchronous span end.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                span_rows = [json.loads(line) for line in traces.read_text().splitlines()] if traces.exists() else []
                if len(span_rows) >= 6:
                    break
                time.sleep(.02)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    (output_dir / "service.prom").write_text(metrics)
    responses = {"health": json.loads(health_body), "work": json.loads(work_body), "failure": json.loads(error_body), "unknown": json.loads(unknown_body)}
    (output_dir / "responses.json").write_text(json.dumps(responses, indent=2))
    log_rows = []
    for line in log_path.read_text().splitlines():
        try:
            log_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    expected_correlations = [(200, responses["work"]["trace_id"]), (500, responses["failure"]["trace_id"])]
    correlated = all(any(row.get("event") == "request_completed" and row.get("route") == "/work"
                         and row.get("status_code") == status and row.get("trace_id") == trace_id for row in log_rows)
                     and any(row["trace_id"] == trace_id and row["name"] == "GET /work" for row in span_rows)
                     for status, trace_id in expected_correlations)
    checks = [
        {"name": "Generated service responds to health and work", "passed": health_status == 200 and work_status == 200, "detail": "A generated Python subprocess served real HTTP requests."},
        {"name": "OpenTelemetry preserves incoming trace context", "passed": responses["work"]["trace_id"] == expected_trace and any(row["trace_id"] == expected_trace and row["parent_span_id"] == "0123456789abcdef" for row in span_rows), "detail": "Incoming W3C traceparent matches the exported SERVER span."},
        {"name": "Failure emits metric and error span", "passed": error_status == 500 and any(row["status"] == "ERROR" for row in span_rows) and 'status_code="500"' in metrics, "detail": "GET /work?fail=1 produced status 500 and OpenTelemetry ERROR."},
        {"name": "Success and failure logs correlate with response and span", "passed": correlated, "detail": "Parsed service.jsonl records match each /work response trace ID, status and exported server span."},
        {"name": "Unknown routes use one bounded label", "passed": unknown_status == 404 and 'route="__unmatched__"' in metrics and "very-specific-customer" not in metrics, "detail": "Customer-specific path is absent from metric labels."},
        {"name": "Invalid contracts fail before generating files", "passed": len(rejected) == len(invalid_cases), "detail": ", ".join(rejected)},
        {"name": "Generation is byte reproducible", "passed": reproducible, "detail": "%d generated files have identical SHA-256 manifests." % len(manifest)},
        {"name": "Dashboard, alerts, Fluent Bit and Docker assets exist", "passed": all((generated / name).exists() for name in ("grafana-dashboard.json", "prometheus-alerts.yaml", "fluent-bit.conf", "Dockerfile")), "detail": "Onboarding includes collection and ownership, not only application code."},
        {"name": "Metrics endpoint exposes histogram samples", "passed": metrics_status == 200 and "service_http_request_duration_seconds_bucket" in metrics, "detail": "Fetched live Prometheus exposition from the generated service."},
    ]
    return {"id": ID, "title": TITLE, "summary": "Turn an ownership and telemetry contract into a working instrumented Python service with repeatable assets.", "checks": checks,
            "measurements": {"generated_files": len(manifest), "rejected_invalid_contracts": len(rejected), "exported_spans": len(span_rows), "http_requests": 5, "json_log_records": len(log_rows),
                             "metric_bytes": len(metrics.encode()), "execution_seconds": round(time.perf_counter() - started, 4)},
            "timeline": [{"phase": "Service owner", "detail": "A small JSON contract declares owner, routes, objectives, and bounded labels."},
                         {"phase": "Generation", "detail": "Python service, Dockerfile, Grafana dashboard, alerts and Fluent Bit configuration appear together."},
                         {"phase": "First request", "detail": "W3C context crosses the HTTP boundary into real OpenTelemetry spans."},
                         {"phase": "First failure", "detail": "An injected 500 appears in metrics, correlated logs, and an ERROR span."},
                         {"phase": "Governance", "detail": "Invalid labels and paths are rejected; deterministic manifests make review easy."}],
            "scenarios": ["Contract validation", "Reproducible generation", "Health and work routes", "W3C trace propagation", "Injected 500", "Unknown path cardinality", "Prometheus histogram", "JSON log correlation", "Fluent Bit SQLite WAL and filesystem queue configuration", "Docker and Grafana onboarding assets"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/10"))
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.contract:
        if not args.destination:
            parser.error("--destination is required with --contract")
        print(json.dumps(generate(json.loads(args.contract.read_text()), args.destination), indent=2))
    else:
        print(json.dumps(run_demo(args.output), indent=2))
