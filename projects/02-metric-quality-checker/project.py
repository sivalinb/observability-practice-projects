"""Metric contracts, label policy, real cardinality counting and rollout modes."""
from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path


HERE = Path(__file__).resolve().parent


def series_multiplier(definition: dict) -> int:
    # A classic histogram has finite buckets + +Inf + _sum + _count.
    return len(definition.get("buckets", [])) + 3 if definition["type"] == "histogram" else 1


def validate(contract: dict, snapshot: dict, mode: str = "enforce", as_of: date | None = None) -> dict:
    if mode not in {"warn", "enforce"}:
        raise ValueError("mode must be warn or enforce")
    as_of = as_of or date.today()
    issues: list[dict] = []
    measurements = []
    definitions = contract["metrics"]

    def issue(name: str, rule: str, detail: str):
        issues.append({"metric": name, "rule": rule, "detail": detail})

    seen_descriptors = set()
    for metric in snapshot["metrics"]:
        name = metric["name"]
        if name in seen_descriptors:
            issue(name, "duplicate_descriptor", "One descriptor per metric is required; aggregate samples before validation.")
        seen_descriptors.add(name)
        if name not in definitions:
            issue(name, "unregistered_metric", "Metric has no approved contract.")
            continue
        definition = definitions[name]
        for field in ("type", "unit", "owner"):
            if metric.get(field) != definition[field]:
                issue(name, field, f"Expected {definition[field]!r}, observed {metric.get(field)!r}.")
        expected_suffix = definition.get("required_suffix")
        if expected_suffix and not name.endswith(expected_suffix):
            issue(name, "name_suffix", f"Expected metric suffix {expected_suffix}.")
        retired_on = definition.get("remove_after")
        if retired_on and as_of >= date.fromisoformat(retired_on):
            issue(name, "deprecated_schema", f"Deprecated metric expired on {retired_on}; use {definition['replacement']}.")
        if definition["type"] == "histogram" and metric.get("buckets") != definition["buckets"]:
            issue(name, "histogram_buckets", "Bucket boundaries differ from the contract; aggregation would become misleading.")
        observed_keys = set()
        for sample in metric.get("samples", []):
            labels = sample.get("labels", {})
            observed_keys.add(tuple(sorted((str(key), str(value)) for key, value in labels.items())))
            for key in labels:
                if key in contract["forbidden_labels"]:
                    issue(name, "forbidden_label", f"Label {key!r} is forbidden; put per-request identity in traces or logs.")
                elif key not in definition["labels"]:
                    issue(name, "unknown_label", f"Label {key!r} is not registered.")
            for key, values in definition["labels"].items():
                if key not in labels:
                    issue(name, "missing_label", f"Required label {key!r} is missing.")
                elif labels[key] not in values:
                    issue(name, "label_value", f"{key}={labels[key]!r} is outside its bounded vocabulary.")
            value = sample.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                issue(name, "sample_value", "Samples must be finite numbers.")
            elif definition["type"] in {"counter", "histogram"} and value < 0:
                issue(name, "negative_value", "This counter or latency observation cannot be negative.")
        multiplier = series_multiplier(definition)
        observed = len(observed_keys) * multiplier
        upper_bound = math.prod(len(values) for values in definition["labels"].values()) * multiplier
        if observed > definition["max_series"]:
            issue(name, "observed_cardinality", f"Observed {observed} wire series exceeds budget {definition['max_series']}.")
        if upper_bound > definition["max_series"]:
            issue(name, "contract_cardinality", f"Declared combinations allow {upper_bound} wire series; budget is {definition['max_series']}.")
        measurements.append({"metric": name, "observed_label_sets": len(observed_keys), "wire_series_per_label_set": multiplier,
                             "observed_series": observed, "declared_upper_bound": upper_bound, "series_budget": definition["max_series"]})
    # Collapse repeated violations so one bad label cannot flood diagnostics.
    unique_issues = list({json.dumps(item, sort_keys=True): item for item in issues}.values())
    return {"schema_version": contract["schema_version"], "mode": mode, "as_of": as_of.isoformat(), "accepted": mode == "warn" or not unique_issues,
            "violation_count": len(unique_issues), "issues": unique_issues, "cardinality": measurements}


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = json.loads((HERE / "contract.json").read_text())
    before = json.loads((HERE / "snapshot-before.json").read_text())
    after = json.loads((HERE / "snapshot-after.json").read_text())
    as_of = date(2026, 10, 18)
    blocked = validate(contract, before, "enforce", as_of)
    warned = validate(contract, before, "warn", as_of)
    repaired = validate(contract, after, "enforce", as_of)
    (output_dir / "before-validation.json").write_text(json.dumps(blocked, indent=2) + "\n")
    (output_dir / "warn-validation.json").write_text(json.dumps(warned, indent=2) + "\n")
    (output_dir / "after-validation.json").write_text(json.dumps(repaired, indent=2) + "\n")
    rules = {issue["rule"] for issue in blocked["issues"]}
    before_hist = next(item for item in blocked["cardinality"] if item["metric"] == "http_request_duration_seconds")
    after_hist = next(item for item in repaired["cardinality"] if item["metric"] == "http_request_duration_seconds")
    # Duplicating a sample must not create a new observed time series.
    repeated = json.loads(json.dumps(after))
    repeated["metrics"][0]["samples"].append(repeated["metrics"][0]["samples"][0])
    repeat_result = validate(contract, repeated, "enforce", as_of)
    repeat_hist = repeat_result["cardinality"][0]
    checks = [
        {"name": "Bad snapshot blocked in enforce mode", "passed": not blocked["accepted"] and blocked["violation_count"] > 0, "detail": f"{blocked['violation_count']} distinct contract violations."},
        {"name": "Warn mode admits and reports", "passed": warned["accepted"] and warned["issues"] == blocked["issues"], "detail": "Same diagnostics are emitted while ingestion remains permitted."},
        {"name": "Repaired snapshot accepted", "passed": repaired["accepted"] and repaired["violation_count"] == 0, "detail": "Bounded vocabularies, metadata, metric names and histogram buckets match the contract."},
        {"name": "Unbounded identity caught", "passed": {"forbidden_label", "observed_cardinality"} <= rules, "detail": f"Before: {before_hist['observed_series']} histogram wire series; budget {before_hist['series_budget']}."},
        {"name": "Histogram expansion included", "passed": after_hist["wire_series_per_label_set"] == 7 and after_hist["observed_series"] == 14 and after_hist["declared_upper_bound"] == 126, "detail": "Four finite buckets + +Inf + sum + count = seven wire series per observed label set."},
        {"name": "Expired schema detected", "passed": "deprecated_schema" in rules, "detail": "2026-10-18 evaluation blocks the legacy metric retired on 2026-10-01."},
        {"name": "Metadata and bucket drift detected", "passed": {"owner", "unit", "histogram_buckets", "unregistered_metric"} <= rules, "detail": "Ownership, unit, bucket layout and unregistered metrics are independently validated."},
        {"name": "Duplicate observations do not inflate series", "passed": repeat_hist["observed_series"] == after_hist["observed_series"], "detail": f"Repeated same-label observation still yields {repeat_hist['observed_series']} series."},
    ]
    return {"id": "02", "title": "Metric Quality Checker", "summary": "A versioned metric contract blocks unsafe labels and schema drift, distinguishes observed series from declared capacity, and supports a warn-first rollout.",
            "checks": checks, "measurements": {"before_violations": blocked["violation_count"], "after_violations": repaired["violation_count"], "histogram_series_before": before_hist["observed_series"], "histogram_series_after": after_hist["observed_series"], "histogram_declared_upper_bound": after_hist["declared_upper_bound"], "histogram_wire_multiplier": after_hist["wire_series_per_label_set"]},
            "timeline": [{"phase": "Contract", "detail": "Version schema, owner, units, label vocabularies, histogram boundaries and removal dates."}, {"phase": "Observe", "detail": f"The bad fixture creates {before_hist['observed_series']} histogram series and {blocked['violation_count']} distinct violations."}, {"phase": "Warn", "detail": "Emit the same evidence while allowing traffic, giving owners a migration window."}, {"phase": "Enforce", "detail": f"The repaired fixture passes with {after_hist['observed_series']} observed histogram series versus {after_hist['declared_upper_bound']} declared maximum."}],
            "scenarios": ["forbidden per-user label", "unregistered metric and label", "missing bounded label", "unit and owner mismatch", "histogram bucket drift", "observed cardinality over budget", "declared upper-bound calculation", "histogram wire-series expansion", "expired schema replacement", "warn versus enforce", "duplicate observation stability", "clean snapshot acceptance"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=HERE / "contract.json")
    parser.add_argument("--snapshot", type=Path, default=HERE / "snapshot-before.json")
    parser.add_argument("--mode", choices=["warn", "enforce"], default="enforce")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    arguments = parser.parse_args()
    result = validate(json.loads(arguments.contract.read_text()), json.loads(arguments.snapshot.read_text()), arguments.mode, arguments.as_of)
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
