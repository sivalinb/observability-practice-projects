#!/usr/bin/env python3
"""Save reproducible public snapshots of the local synthetic execution evidence."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/evidence"


def fetch(url, **kwargs):
    response = requests.get(url, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    reports = fetch("http://localhost:18000/api/reports")
    (OUT / "project-reports.json").write_text(json.dumps(reports, indent=2) + "\n")
    queries = []
    for report in reports:
        identifier = report["id"]
        expression = f'prep_measurement{{project="{identifier}"}}'
        response = fetch("http://localhost:13000/api/datasources/proxy/uid/prometheus/api/v1/query", params={"query": expression})
        queries.append({"project": identifier, "query": expression, "response": response})
    (OUT / "grafana-measurement-queries.json").write_text(json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat(), "queries": queries}, indent=2) + "\n")
    screenshots = []
    for report in reports:
        project = next((ROOT / "projects").glob(report["id"] + "-*"))
        image = project / "assets/demo.jpg"
        if not image.exists():
            continue
        screenshots.append({"project": report["id"], "source": "Grafana", "dashboard_uid": "prep" + report["id"],
                            "url": "http://localhost:13000/d/prep" + report["id"],
                            "file": str(image.relative_to(ROOT)), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                            "captured_at": datetime.fromtimestamp(image.stat().st_mtime, timezone.utc).isoformat(),
                            "report_run_id": report["run_id"]})
    (OUT / "screenshots.json").write_text(json.dumps(screenshots, indent=2) + "\n")
    print(f"Recorded {len(reports)} project reports, {len(queries)} Grafana queries and {len(screenshots)} screenshots.")


if __name__ == "__main__":
    main()
