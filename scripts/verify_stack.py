#!/usr/bin/env python3
"""Verify real APIs. --chaos stops/restarts only this Compose project's services."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stack import compose

RESULTS = []


def command(*args):
    completed = subprocess.run(compose() + ["-f", str(ROOT / "compose.yaml"), "--profile", "replay"] + list(args), cwd=ROOT, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-1800:] or completed.stdout[-1800:])
    return completed.stdout


def wait_for(predicate, timeout=90):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = predicate()
            if last:
                return last
        except (requests.RequestException, ValueError, KeyError) as error:
            last = str(error)
        time.sleep(1)
    raise AssertionError(f"Condition not met within {timeout}s: {last}")


def get(url, **kwargs):
    response = requests.get(url, timeout=5, **kwargs)
    response.raise_for_status()
    return response


def check(name, detail):
    RESULTS.append({"name": name, "passed": True, "detail": detail})
    print("PASS " + name + ": " + str(detail), flush=True)


def query(expression):
    return get("http://localhost:19090/api/v1/query", params={"query": expression}).json()["data"]["result"]


def loki_count(marker, source=None):
    selector = '{job="observability-practice"' + (',source="' + source + '"' if source else '') + '} |= "' + marker + '"'
    result = get("http://localhost:13100/loki/api/v1/query_range", params={"query": selector, "limit": 1000, "start": str(time.time_ns() - 3600 * 10**9)}).json()["data"]["result"]
    identifiers = set()
    for stream in result:
        for _, line in stream["values"]:
            row = json.loads(line)
            event_id = row.get("event_id", "")
            if event_id.startswith(marker + "-"):
                identifiers.add(event_id)
    # A duplicate must never stand in for a missing canary.
    return len(identifiers & {f"{marker}-{i}" for i in range(5)})


def append_logs(marker, count=5, kafka=False):
    records = [{"event_id": f"{marker}-{i}", "project": "01", "service": "observability-practice", "scenario": marker,
                "sequence": i, "timestamp": time.time(), "passed": True} for i in range(count)]
    path = "/app/data/logs/" + ("kafka.jsonl" if kafka else "app.jsonl")
    code = "import os,json;from pathlib import Path;p=Path(" + repr(path) + ");p.parent.mkdir(parents=True,exist_ok=True);f=p.open('a');f.write(" + repr("".join(json.dumps(r) + "\n" for r in records)) + ");f.flush();os.fsync(f.fileno());f.close()"
    command("exec", "-T", "prep", "python", "-c", code)
    return records


def trace_canaries(marker, count):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    provider = TracerProvider(resource=Resource.create({"service.name": "stack-canary"}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:14318/v1/traces", timeout=10)))
    tracer, ids = provider.get_tracer("verification"), []
    for i in range(count):
        with tracer.start_as_current_span(marker, attributes={"sequence": i}) as span:
            ids.append(format(span.get_span_context().trace_id, "032x"))
    provider.force_flush()
    provider.shutdown()
    return ids


def traces_present(ids):
    return all(requests.get("http://localhost:13200/api/traces/" + identifier, timeout=5).status_code == 200 for identifier in ids)


def verify_base():
    wait_for(lambda: get("http://localhost:18000/health").json().get("projects") == 10)
    reports = get("http://localhost:18000/api/reports").json()
    assert len(reports) == 10 and all(c["passed"] for r in reports for c in r["checks"])
    expected = sum(len(r["checks"]) for r in reports)
    check("All ten Python project demos", f"{expected} measured checks passed")
    wait_for(lambda: query("sum(prep_check_result)") and float(query("sum(prep_check_result)")[0]["value"][1]) == expected)
    check("Prometheus scrapes actual project evidence", f"sum(prep_check_result) = {expected}")
    wait_for(lambda: query("sum(prep_check_runs_total)") and float(query("sum(prep_check_runs_total)")[0]["value"][1]) >= expected)
    check("OTel metric export reaches Prometheus", "OTLP check counter is queryable")
    wait_for(lambda: len(query("prep_sli_requests_total")) == 1)
    check("Wall-clock SLI source is scraped", "Live counters are separate from accelerated logical-window tests")
    wait_for(lambda: get("http://localhost:13100/ready").status_code == 200)
    wait_for(lambda: get("http://localhost:13200/ready").status_code == 200)
    marker = "verify-" + uuid.uuid4().hex[:12]
    append_logs(marker)
    wait_for(lambda: loki_count(marker) >= 5)
    check("Fluent Bit tail to Loki query", "Five unique canary log records arrived through Fluent Bit")
    ids = trace_canaries(marker, 2)
    wait_for(lambda: traces_present(ids))
    check("OTel trace export to Tempo", "Two known trace IDs are retrievable from Tempo")
    for uid in ("prometheus", "loki"):
        health = get(f"http://localhost:13000/api/datasources/uid/{uid}/health").json()
        assert health["status"] == "OK", health
    # Tempo's built-in plugin does not implement the /health resource in this
    # Grafana version; exercise its provisioned backend proxy instead.
    assert get("http://localhost:13000/api/datasources/proxy/uid/tempo/ready").status_code == 200
    for report in reports:
        identifier = report["id"]
        actual = get("http://localhost:13000/api/datasources/proxy/uid/prometheus/api/v1/query", params={"query": f'prep_measurement{{project="{identifier}"}}'}).json()["data"]["result"]
        actual_values = {item["metric"]["measurement"]: float(item["value"][1]) for item in actual}
        expected_values = {key: float(value) for key, value in report["measurements"].items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
        assert actual_values.keys() == expected_values.keys(), (identifier, actual_values, expected_values)
        assert all(actual_values[key] == value for key, value in expected_values.items()), identifier
        log_query = '{job="observability-practice",project="' + identifier + '"} |= "' + report["run_id"] + '"'
        logs = get("http://localhost:13000/api/datasources/proxy/uid/loki/loki/api/v1/query_range", params={"query": log_query, "limit": 1000, "start": str(time.time_ns() - 3600*10**9)}).json()["data"]["result"]
        logged_scenarios = {json.loads(line).get("scenario") for stream in logs for _, line in stream["values"]}
        assert {check["name"] for check in report["checks"]}.issubset(logged_scenarios), identifier
    check("All ten projects verified through Grafana data-source proxies", "Every numeric measurement equals its raw report; latest-run log evidence exists for every scenario")
    dashboards = get("http://localhost:13000/api/search", params={"type": "dash-db"}).json()
    assert {"prep" + f"{i:02d}" for i in range(1, 11)}.issubset({d["uid"] for d in dashboards})
    check("Grafana provisioned data sources and dashboards", f"Three data sources healthy; {len(dashboards)} dashboards")
    command("cp", "projects/04-service-health-alerts/prometheus-rules.test.yml", "prometheus:/tmp/prometheus-rules.test.yml")
    command("cp", "projects/04-service-health-alerts/prometheus-rules.yml", "prometheus:/tmp/prometheus-rules.yml")
    validation = command("exec", "-T", "prometheus", "promtool", "check", "rules", "/tmp/prometheus-rules.yml")
    validation += command("exec", "-T", "-w", "/tmp", "prometheus", "promtool", "test", "rules", "prometheus-rules.test.yml")
    check("Prometheus rule validation and time-window fixtures", validation.strip())


def verify_chaos():
    marker = "otel-restart-" + uuid.uuid4().hex[:12]
    command("stop", "tempo")
    try:
        ids = trace_canaries(marker, 5)
        # With no batch processor, accepted exporter requests enter file_storage directly.
        command("kill", "-s", "SIGKILL", "otel")
        command("start", "otel")
    finally:
        command("start", "tempo")
    wait_for(lambda: traces_present(ids), timeout=120)
    check("OTel persistent queue survives SIGKILL during backend outage", "All five accepted traces recovered after Collector kill and Tempo restoration")
    marker = "fluent-restart-" + uuid.uuid4().hex[:12]
    command("stop", "loki")
    try:
        append_logs(marker)
        time.sleep(3)  # permit tail discovery and a failed HTTP flush to form disk chunks
        command("kill", "-s", "SIGKILL", "fluent-bit")
        command("start", "fluent-bit")
    finally:
        command("start", "loki")
    wait_for(lambda: loki_count(marker) >= 5, timeout=120)
    check("Fluent Bit filesystem buffer survives SIGKILL during Loki outage", "All five canary records recovered; duplicates are permitted")


def verify_kafka():
    from kafka import KafkaConsumer, TopicPartition
    marker = "kafka-" + uuid.uuid4().hex[:12]
    command("stop", "kafka-bridge")
    try:
        records = append_logs(marker, kafka=True)
        # Establish broker persistence before restarting the consumer.
        consumer = KafkaConsumer(bootstrap_servers="localhost:29092", enable_auto_commit=False, auto_offset_reset="earliest", consumer_timeout_ms=1000)
        wait_for(lambda: "prep-events" in consumer.topics())
        partition = TopicPartition("prep-events", 0)
        consumer.assign([partition])
        consumer.seek_to_beginning()
        found = set()
        end = time.monotonic() + 45
        while time.monotonic() < end and len(found) < 5:
            for batches in consumer.poll(timeout_ms=1000).values():
                for message in batches:
                    row = json.loads(message.value)
                    if row.get("scenario") == marker:
                        found.add(row["event_id"])
        assert found == {r["event_id"] for r in records}, found
        consumer.close()
    finally:
        command("start", "kafka-bridge")
    wait_for(lambda: loki_count(marker, "kafka") >= 5)
    consumer = KafkaConsumer(bootstrap_servers="localhost:29092", group_id="prep-loki-bridge", enable_auto_commit=False)
    partition = TopicPartition("prep-events", 0)
    end_offset = consumer.end_offsets([partition])[partition]
    wait_for(lambda: (consumer.committed(partition) or 0) >= end_offset)
    committed = consumer.committed(partition)
    consumer.close()
    check("Fluent Bit to Kafka and consumer outage replay", f"Five records retained while consumer stopped; Loki received them after restart; committed offset {committed} >= {end_offset}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chaos", action="store_true")
    parser.add_argument("--kafka", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/stack-verification.json")
    args = parser.parse_args()
    error = None
    try:
        verify_base()
        if args.chaos:
            verify_chaos()
        if args.kafka:
            verify_kafka()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        RESULTS.append({"name": "Integration execution", "passed": False, "detail": error})
        print("FAIL " + error, file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"executed_at": datetime.now(timezone.utc).isoformat(), "checks": RESULTS}, indent=2) + "\n")
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
