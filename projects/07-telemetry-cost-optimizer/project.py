"""Measure telemetry reduction against incident detection, using actual fixture bytes."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

ID = "07"
TITLE = "Telemetry Cost Optimizer"
BUCKETS = (50, 100, 250, 500, 1000, 2500, float("inf"))


def fixture(seed=71):
    """Create reproducible requests with two short incidents and one rare failure."""
    rng = random.Random(seed)
    rows = []
    for minute in range(30):
        for index in range(100):
            latency = max(4, rng.lognormvariate(4.1, .35))
            status = 200
            if 8 <= minute < 11 and index % 3 == 0:
                status, latency = 503, latency + 80
            if 18 <= minute < 21 and index % 4 == 0:
                latency += 1250
            if minute == 25 and index == 48:
                status, latency = 500, 2050
            rows.append({
                "timestamp": minute * 60 + index * .6,
                "minute": minute,
                "service": "checkout",
                "tenant": "north" if index % 2 else "south",
                "route": "/checkout",
                "status": status,
                "latency_ms": round(latency, 3),
                "request_id": hashlib.sha256((str(seed) + ":" + str(minute) + ":" + str(index)).encode()).hexdigest()[:24],
                "debug": "request parsing completed; auth accepted; upstream attempt observed; response serialized; " * 3,
            })
    return rows


def histogram(values):
    return {str(bound): sum(value <= bound for value in values) for bound in BUCKETS}


def histogram_quantile(q, cumulative):
    """Prometheus-style linear interpolation over cumulative, compatible buckets."""
    ordered = sorted((float(bound), count) for bound, count in cumulative.items())
    if not ordered or ordered[-1][1] == 0:
        return None
    rank = q * ordered[-1][1]
    previous_bound, previous_count = 0.0, 0
    for bound, count in ordered:
        if count >= rank:
            if math.isinf(bound):
                return previous_bound
            if count == previous_count:
                return bound
            return previous_bound + (bound - previous_bound) * (rank - previous_count) / (count - previous_count)
        previous_bound, previous_count = bound, count
    return ordered[-2][0]


def aggregate(rows, window=60):
    groups = defaultdict(list)
    for row in rows:
        groups[int(row["timestamp"] // window) * window].append(row)
    return [{
        "start": start,
        "window_seconds": window,
        "requests": len(values),
        "errors": sum(row["status"] >= 500 for row in values),
        "slow": sum(row["latency_ms"] > 1000 for row in values),
        "latency_sum_ms": round(sum(row["latency_ms"] for row in values), 3),
        "latency_buckets": histogram([row["latency_ms"] for row in values]),
    } for start, values in sorted(groups.items())]


def diagnose(aggregates):
    found = set()
    for row in aggregates:
        minute = int(row["start"] // 60)
        requests = row["requests"]
        if not requests:
            continue
        if row["errors"] / requests >= .10:
            found.add("error-wave")
        if row["slow"] / requests >= .10:
            found.add("slow-dependency")
        if row["errors"] > 0 and 25 <= minute < 26:
            found.add("rare-failure")
    return sorted(found)


def encode(rows):
    return b"".join((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode() for row in rows)


def apply_policy(rows, name):
    if name == "full":
        return rows
    if name == "uniform-10-percent":
        return [row for row in rows if int(row["request_id"][:8], 16) % 10 == 0]
    if name == "incident-aware":
        # Event exemplars are sampled. Independent counters/histograms remain unsampled.
        return [{key: value for key, value in row.items() if key != "debug"}
                for row in rows if row["status"] >= 500 or row["latency_ms"] > 1000
                or int(row["request_id"][:8], 16) % 10 == 0]
    if name == "minute-aggregates":
        return aggregate(rows)
    if name == "five-minute-aggregates":
        return aggregate(rows, 300)
    raise ValueError("Unknown policy " + name)


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    rows = fixture()
    truth = diagnose(aggregate(rows))
    reports = {}
    raw_size = len(encode(rows))
    for name in ("full", "uniform-10-percent", "incident-aware", "minute-aggregates", "five-minute-aggregates"):
        transformed = apply_policy(rows, name)
        encoded = encode(transformed)
        compressed = gzip.compress(encoded, mtime=0)
        (output_dir / (name + ".jsonl.gz")).write_bytes(compressed)
        independent_metrics_bytes = 0
        if "aggregates" in name:
            diagnoses = diagnose(transformed)
        elif name == "incident-aware":
            # Sampled exemplars are never used as rate denominators.
            diagnoses = diagnose(aggregate(rows))
            (output_dir / "incident-aware-unsampled-metrics.json").write_text(json.dumps(aggregate(rows), indent=2))
            metrics_gzip = gzip.compress(encode(aggregate(rows)), mtime=0)
            (output_dir / "incident-aware-unsampled-metrics.jsonl.gz").write_bytes(metrics_gzip)
            independent_metrics_bytes = len(metrics_gzip)
        else:
            diagnoses = diagnose(aggregate(transformed))
        reports[name] = {
            "retained_records": len(transformed), "serialized_bytes": len(encoded),
            "event_gzip_bytes": len(compressed), "independent_metric_gzip_bytes": independent_metrics_bytes,
            "gzip_bytes": len(compressed) + independent_metrics_bytes,
            "reduction_vs_full_jsonl_percent": round(100 * (1 - (len(compressed) + independent_metrics_bytes) / raw_size), 3),
            "diagnoses": diagnoses, "missed_diagnoses": sorted(set(truth) - set(diagnoses)),
            "diagnostic_resolution_seconds": 300 if name == "five-minute-aggregates" else 60,
            "billing_scope": "synthetic ingest only; storage/query/network excluded",
        }
    # Demonstrate why per-shard p95 averages cannot be merged as quantiles.
    shard_a = [10.0] * 1000
    shard_b = [1200.0] * 10
    quantile = lambda values: sorted(values)[math.ceil(.95 * len(values)) - 1]
    mean_p95 = statistics.mean((quantile(shard_a), quantile(shard_b)))
    correct_p95 = quantile(shard_a + shard_b)
    merged = {key: histogram(shard_a)[key] + histogram(shard_b)[key] for key in histogram(shard_a)}
    exact_merged = histogram(shard_a + shard_b)
    # A budget model, not a quoted vendor price. Extrapolate observed fixture rate.
    assumed_dollars_gb = .25
    projection_factor = (30 * 24 * 60 * 60) / (30 * 60)
    for report in reports.values():
        report["modeled_30d_gzip_gb"] = round(report["gzip_bytes"] * projection_factor / 1e9, 6)
        report["modeled_30d_ingest_dollars"] = round(report["modeled_30d_gzip_gb"] * assumed_dollars_gb, 6)
    evidence = {
        "fixture": {"requests": len(rows), "duration_seconds": 1800, "ground_truth": truth},
        "policies": reports,
        "quantile_example": {"mean_of_shard_p95_ms": mean_p95, "actual_combined_p95_ms": correct_p95,
                             "merged_histogram_p95_estimate_ms": histogram_quantile(.95, merged),
                             "histogram_interpolation_note": "An estimate within the bucket, not the raw sample quantile."},
        "assumptions": {"dollars_per_ingested_gb": assumed_dollars_gb, "days": 30, "gb_bytes": 1e9,
                        "rate_is_constant": True, "gzip_is_transmitted": True},
    }
    (output_dir / "cost-and-diagnosis.json").write_text(json.dumps(evidence, indent=2))
    checks = [
        {"name": "Full telemetry finds all injected incidents", "passed": len(truth) == 3, "detail": ", ".join(truth)},
        {"name": "Minute histograms preserve diagnoses", "passed": reports["minute-aggregates"]["diagnoses"] == truth, "detail": str(reports["minute-aggregates"]["diagnoses"])},
        {"name": "Uniform sampling exposes a rare-event blind spot", "passed": "rare-failure" in reports["uniform-10-percent"]["missed_diagnoses"], "detail": "In this fixed fixture the rare error is not selected; a 10% sample is not an event guarantee."},
        {"name": "Incident-aware exemplars retain unsampled denominators", "passed": reports["incident-aware"]["diagnoses"] == truth, "detail": "Metrics are independent from sampled exemplar events."},
        {"name": "Compatible cumulative histograms merge exactly", "passed": merged == exact_merged, "detail": "Sum matching bucket counts; estimate quantiles only after merging."},
        {"name": "Averaging p95 is caught as unsafe", "passed": mean_p95 != correct_p95, "detail": "Mean shard p95 %.1f ms vs combined p95 %.1f ms." % (mean_p95, correct_p95)},
        {"name": "Policy reduces measured bytes", "passed": reports["minute-aggregates"]["gzip_bytes"] < reports["full"]["gzip_bytes"], "detail": "%d vs %d gzip bytes" % (reports["minute-aggregates"]["gzip_bytes"], reports["full"]["gzip_bytes"])},
    ]
    return {"id": ID, "title": TITLE, "summary": "Compare measured bytes with diagnostic coverage before changing retention or sampling.",
            "checks": checks, "measurements": {"requests": len(rows), "baseline_gzip_bytes": reports["full"]["gzip_bytes"],
            "aggregate_gzip_bytes": reports["minute-aggregates"]["gzip_bytes"], "mean_shard_p95_ms": mean_p95,
            "correct_combined_p95_ms": correct_p95, "execution_seconds": round(time.perf_counter() - started, 4)},
            "timeline": [{"phase": "Traffic", "detail": "3,000 requests cross normal, error, slow, and rare-failure periods."},
                         {"phase": "Budget choices", "detail": "Five actual transformations write gzip artifacts."},
                         {"phase": "Replay diagnoses", "detail": "The same rules examine each retained representation."},
                         {"phase": "Decision", "detail": "Keep unsampled metrics; sample detail with explicit coverage trade-offs."}],
            "scenarios": ["Normal traffic", "Sustained errors", "Slow dependency", "Rare failure", "Uniform event sampling", "Incident-aware exemplar sampling", "Minute and five-minute aggregation", "Unsafe quantile averaging", "Measured gzip size and explicit pricing assumptions"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/07"))
    arguments = parser.parse_args()
    print(json.dumps(run_demo(arguments.output), indent=2))
