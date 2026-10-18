"""HTTP trace propagation, completed-trace sampling, and measured Python profiles."""
from __future__ import annotations

import cProfile
import contextvars
import io
import json
import os
import pstats
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


def cpu_hotspot(iterations: int = 100000) -> int:
    value = 17
    for number in range(iterations):
        value = (value * 31 + number * number) % 1000000007
    return value


def parse_traceparent(carrier: dict) -> tuple[str, str] | None:
    value = carrier.get("traceparent", "")
    parts = value.split("-")
    if len(parts) != 4 or parts[0] != "00" or len(parts[1]) != 32 or len(parts[2]) != 16 or len(parts[3]) != 2:
        return None
    try:
        if int(parts[1], 16) == 0 or int(parts[2], 16) == 0:
            return None
        int(parts[3], 16)
    except ValueError:
        return None
    return parts[1], parts[2]


class SpanRecorder:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        self.current = contextvars.ContextVar("performance_trace", default=None)
        self.provider = self.tracer = self.propagator = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
            self.propagator = TraceContextTextMapPropagator()
            if isinstance(trace.get_tracer_provider(), TracerProvider):
                self.tracer = trace.get_tracer("performance-troubleshooter")
            else:
                self.provider = TracerProvider(resource=Resource.create({"service.name": "performance-troubleshooter"}))
                self.tracer = self.provider.get_tracer("performance-troubleshooter")
                endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                if endpoint:
                    from opentelemetry.sdk.trace.export import BatchSpanProcessor
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                    self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
        except ImportError:
            # The local diagnostic records remain usable with only Python installed.
            self.provider = self.tracer = self.propagator = None

    @contextmanager
    def span(self, name: str, carrier: dict | None = None, **attributes):
        start = time.perf_counter_ns()
        wall_start = time.time_ns()
        if self.tracer:
            from opentelemetry import trace
            from opentelemetry.trace import Status, StatusCode
            context = self.propagator.extract(carrier) if carrier is not None else None
            parent_context = trace.get_current_span(context).get_span_context()
            with self.tracer.start_as_current_span(name, context=context, attributes=attributes) as sdk_span:
                sdk_context = sdk_span.get_span_context()
                row = {"trace_id": f"{sdk_context.trace_id:032x}", "span_id": f"{sdk_context.span_id:016x}",
                       "parent_id": f"{parent_context.span_id:016x}" if parent_context.is_valid else None,
                       "name": name, "attributes": attributes, "start_unix_nano": wall_start}
                try:
                    yield row
                finally:
                    if row["attributes"].get("error"):
                        sdk_span.set_status(Status(StatusCode.ERROR))
                    row["duration_ms"] = (time.perf_counter_ns() - start) / 1e6
                    with self.lock:
                        self.rows.append(row)
        else:
            remote = parse_traceparent(carrier) if carrier is not None else None
            parent = self.current.get() if carrier is None else None
            row = {"trace_id": remote[0] if remote else parent["trace_id"] if parent else uuid.uuid4().hex,
                   "parent_id": remote[1] if remote else parent["span_id"] if parent else None,
                   "span_id": uuid.uuid4().hex[:16], "name": name, "attributes": attributes,
                   "start_unix_nano": wall_start}
            token = self.current.set(row)
            try:
                yield row
            finally:
                self.current.reset(token)
                row["duration_ms"] = (time.perf_counter_ns() - start) / 1e6
                with self.lock:
                    self.rows.append(row)

    def inject(self) -> dict:
        carrier = {}
        if self.propagator:
            self.propagator.inject(carrier)
        else:
            row = self.current.get()
            if row:
                carrier["traceparent"] = f"00-{row['trace_id']}-{row['span_id']}-01"
        return carrier

    def close(self):
        if self.provider:
            self.provider.force_flush()
            self.provider.shutdown()


def retain_completed_traces(spans: list[dict], slow_ms: float = 25, max_traces: int = 1000) -> dict:
    """Offline completed-trace policy. Collector arrival-time buffering is separate."""
    groups = defaultdict(list)
    for span in spans:
        groups[span["trace_id"]].append(span)
        if len(groups) > max_traces:
            raise BufferError("completed-trace capacity exceeded")
    head = {trace_id: rows for trace_id, rows in groups.items() if int(trace_id, 16) % 4 == 0}
    tail = {trace_id: rows for trace_id, rows in groups.items()
            if any(row["attributes"].get("error") for row in rows)
            or any(row["name"] == "frontend.request" and row["duration_ms"] >= slow_ms for row in rows)}
    return {"all": dict(groups), "head": head, "tail": tail}


def handler_for(recorder: SpanRecorder, dependency_port: int | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            mode = parse_qs(urlsplit(self.path).query).get("mode", ["quick"])[0]
            name = "dependency.request" if dependency_port is None else "frontend.request"
            carrier = {key.lower(): value for key, value in self.headers.items()}
            with recorder.span(name, carrier, mode=mode) as span:
                if dependency_port is None:
                    if mode == "slow":
                        time.sleep(.04)
                    result = {"dependency": "ok"}
                else:
                    with recorder.span("python.cpu_hotspot"):
                        checksum = cpu_hotspot(120000 if mode == "cpu" else 1000)
                    with recorder.span("dependency.client"):
                        request = Request(f"http://127.0.0.1:{dependency_port}/value?mode={mode}", headers=recorder.inject())
                        with urlopen(request, timeout=3) as response:
                            json.load(response)
                    result = {"checksum": checksum, "mode": mode, "trace_id": span["trace_id"]}
                status = 503 if mode == "rare_error" and dependency_port is not None else 200
                span["attributes"]["error"] = status >= 500
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
    return Handler


def measure_overhead(recorder: SpanRecorder, rounds: int = 7, iterations: int = 50) -> dict:
    baseline, instrumented = [], []
    for round_number in range(rounds):
        # Alternate execution order to reduce warmup and scheduler bias.
        for traced in ((False, True) if round_number % 2 == 0 else (True, False)):
            start = time.perf_counter_ns()
            for _ in range(iterations):
                if traced:
                    with recorder.span("overhead_probe"):
                        cpu_hotspot(1000)
                else:
                    cpu_hotspot(1000)
            (instrumented if traced else baseline).append((time.perf_counter_ns() - start) / iterations / 1000)
    baseline_us = statistics.median(baseline)
    traced_us = statistics.median(instrumented)
    return {"baseline_median_us": round(baseline_us, 3), "traced_median_us": round(traced_us, 3),
            "instrumentation_delta_us": round(traced_us - baseline_us, 3),
            "instrumentation_delta_percent": round((traced_us / baseline_us - 1) * 100, 2)}


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    recorder = SpanRecorder()
    dependency = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(recorder))
    frontend = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(recorder, dependency.server_port))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (dependency, frontend)]
    for thread in threads:
        thread.start()
    try:
        responses = []
        # Preserve deterministic low bits for the sampling exercise, while each
        # execution gets distinct trace IDs instead of merging unrelated runs.
        trace_base = (uuid.uuid4().int >> 32) << 32
        for identifier in range(1, 13):
            mode = {5: "slow", 7: "rare_error", 9: "cpu"}.get(identifier, "quick")
            traceparent = f"00-{trace_base + identifier:032x}-{identifier + 100:016x}-01"
            request = Request(f"http://127.0.0.1:{frontend.server_port}/work?mode={mode}", headers={"traceparent": traceparent})
            try:
                with urlopen(request, timeout=5) as response:
                    responses.append({"status": response.status, **json.load(response)})
            except HTTPError as response:
                responses.append({"status": response.code, **json.load(response)})
        # HTTP response delivery can precede the outer span's finalizer by a scheduling slice.
        deadline = time.monotonic() + 2
        while len(recorder.rows) < 48 and time.monotonic() < deadline:
            time.sleep(.001)
        diagnostic_spans = list(recorder.rows)
        retained = retain_completed_traces(diagnostic_spans)
        # Profile an isolated process: concurrent SDK/HTTP thread teardown can
        # interfere with CPython 3.12's profiler monitoring in some patch releases.
        profile_path = output_dir / "cpu-profile.pstats"
        code = "import cProfile,runpy,sys; ns=runpy.run_path(sys.argv[1]); fn=ns['cpu_hotspot']; p=cProfile.Profile(); p.enable(); [fn(150000) for _ in range(5)]; p.disable(); p.dump_stats(sys.argv[2])"
        subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve()), str(profile_path)], check=True, timeout=30)
        stream = io.StringIO()
        stats = pstats.Stats(str(profile_path), stream=stream).strip_dirs().sort_stats("cumulative")
        stats.print_stats(15)
        profile_text = stream.getvalue()
        (output_dir / "cpu-profile.txt").write_text(profile_text)
        overhead = measure_overhead(recorder)
        propagated = True
        for trace_id, rows in retained["all"].items():
            by_name = {row["name"]: row for row in rows}
            propagated &= len(rows) == 4 and by_name["dependency.request"]["parent_id"] == by_name["dependency.client"]["span_id"]
            propagated &= by_name["dependency.client"]["parent_id"] == by_name["frontend.request"]["span_id"]
        error_id, slow_id = f"{trace_base + 7:032x}", f"{trace_base + 5:032x}"
        checks = [
            {"name": "real_http_frontend_and_dependency", "passed": len(responses) == 12 and sum(r["status"] == 503 for r in responses) == 1, "detail": "12 frontend and 12 dependency requests; one injected HTTP 503"},
            {"name": "parent_child_context_propagation", "passed": bool(propagated) and len(retained["all"]) == 12, "detail": f"{len(diagnostic_spans)} spans linked across two threaded HTTP services"},
            {"name": "rare_error_sampling_tradeoff", "passed": error_id in retained["tail"] and error_id not in retained["head"], "detail": "Fixture head policy drops rare error trace; completed-trace tail policy retains it"},
            {"name": "slow_dependency_retained", "passed": slow_id in retained["tail"], "detail": f"Slow trace duration {next(r['duration_ms'] for r in retained['all'][slow_id] if r['name'] == 'frontend.request'):.2f}ms"},
            {"name": "profile_identifies_cpu_function", "passed": "cpu_hotspot" in profile_text and any(key[2] == "cpu_hotspot" and value[0] == 5 for key, value in stats.stats.items()), "detail": "cProfile measured five cpu_hotspot calls; .pstats and ranked text saved"},
            {"name": "instrumentation_overhead_measured", "passed": overhead["baseline_median_us"] > 0 and overhead["traced_median_us"] > 0, "detail": f"median delta {overhead['instrumentation_delta_us']}µs over seven alternating rounds"},
        ]
        (output_dir / "traces.json").write_text(json.dumps({"spans": diagnostic_spans,
            "head_retained": list(retained["head"]), "tail_retained": list(retained["tail"]),
            "responses": responses, "otel_sdk_enabled": recorder.tracer is not None}, indent=2))
        (output_dir / "overhead.json").write_text(json.dumps(overhead, indent=2))
        return {"id": "06", "title": "Performance Troubleshooter",
                "summary": "Propagate request context across HTTP, compare head and completed-trace tail policies, and use cProfile to explain CPU time.",
                "checks": checks, "measurements": {"http_requests": 24, "trace_count": len(retained["all"]),
                    "span_count": len(diagnostic_spans), "head_retained": len(retained["head"]), "tail_retained": len(retained["tail"]),
                    "otel_sdk_enabled": str(recorder.tracer is not None).lower(), **overhead},
                "timeline": [{"phase": "Follow", "detail": "W3C context crosses frontend, client, and dependency spans."},
                             {"phase": "Compare", "detail": "Head decisions see IDs; completed-trace decisions see errors and latency."},
                             {"phase": "Explain", "detail": "Profiles identify Python CPU work; repeated probes measure instrumentation cost."}],
                "scenarios": [c["name"] for c in checks]}
    finally:
        for server in (frontend, dependency):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        recorder.close()
