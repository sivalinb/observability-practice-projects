"""Load independent projects; retain raw evidence and fail the process on failed checks."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.getenv("ARTIFACT_DIR", ROOT / "artifacts"))
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT / "data/logs"))


def projects():
    return {p.name[:2]: p for p in sorted((ROOT / "projects").glob("[0-9][0-9]-*")) if (p / "project.py").exists()}


def load_project(identifier):
    path = projects()[identifier] / "project.py"
    name = "prep_project_" + identifier
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and postponed annotations need a module identity
    spec.loader.exec_module(module)
    return module


class Telemetry:
    """An OTLP export is optional for local algorithm runs, required in Compose."""

    def __init__(self):
        self.tracer = self.counter = self.provider = self.meter_provider = None
        if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        endpoint = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"].rstrip("/")
        resource = Resource.create({"service.name": "observability-practice", "service.version": "1.0.0"})
        self.provider = TracerProvider(resource=resource)
        self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint + "/v1/traces")))
        trace.set_tracer_provider(self.provider)
        self.tracer = trace.get_tracer("observability-practice")
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint + "/v1/metrics"), export_interval_millis=2000)
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        self.counter = self.meter_provider.get_meter("observability-practice").create_counter("prep_check_runs", description="Executed scenario checks")

    def span(self, name, **attributes):
        return self.tracer.start_as_current_span(name, attributes=attributes) if self.tracer else nullcontext()

    def flush(self):
        if self.provider:
            self.provider.force_flush()
        if self.meter_provider:
            self.meter_provider.force_flush()

    def shutdown(self):
        if self.provider:
            self.provider.shutdown()
        if self.meter_provider:
            self.meter_provider.shutdown()


def execute(identifier, telemetry=None):
    telemetry = telemetry or Telemetry()
    out = ARTIFACTS / identifier / uuid.uuid4().hex[:12]
    out.mkdir(parents=True)
    started = time.perf_counter()
    try:
        with telemetry.span("project." + identifier, project=identifier):
            report = load_project(identifier).run_demo(out)
    except Exception as error:
        # A failed rerun must replace an older green snapshot with explicit evidence.
        report = {"id": identifier, "title": projects()[identifier].name[3:].replace("-", " ").title(),
                  "summary": "Execution failed; inspect the failed scenario before trusting this run.",
                  "checks": [{"name": "Project execution completed", "passed": False,
                              "detail": f"{type(error).__name__}: {error}"}],
                  "measurements": {}, "timeline": [{"phase": "Execution failed", "detail": type(error).__name__}],
                  "scenarios": ["Execution failure"]}
    with telemetry.span("project.results." + identifier, project=identifier):
        for check in report["checks"]:
            if not isinstance(check.get("passed"), bool):
                raise ValueError("Scenario checks must produce booleans")
            with telemetry.span("check." + check["name"], project=identifier, passed=check["passed"]):
                if telemetry.counter:
                    telemetry.counter.add(1, {"project": identifier, "outcome": "pass" if check["passed"] else "fail"})
    report.update({"id": identifier, "run_id": out.name, "executed_at": datetime.now(timezone.utc).isoformat(),
                   "duration_seconds": round(time.perf_counter() - started, 4), "artifact_path": str(out.relative_to(ARTIFACTS))})
    # No host paths, credentials or user data are written to published examples.
    text = json.dumps(report, indent=2, default=str)
    (out / "report.json").write_text(text + "\n")
    (ARTIFACTS / identifier / "latest.json").write_text(text + "\n")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "app.jsonl").open("a") as log:
        for check in report["checks"]:
            log.write(json.dumps({"timestamp": time.time(), "event_id": uuid.uuid4().hex, "project": identifier,
                                  "service": "observability-practice", "scenario": check["name"], "passed": check["passed"],
                                  "detail": check.get("detail", ""), "run_id": out.name}, default=str) + "\n")
        log.flush()
        os.fsync(log.fileno())
    return report


def reports():
    result = []
    for path in sorted(ARTIFACTS.glob("*/latest.json")):
        try:
            result.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return result
