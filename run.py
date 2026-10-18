#!/usr/bin/env python3
"""One entry point for ten runnable projects and the evidence viewer."""
import argparse
import json
from shared.runner import Telemetry, execute, projects


def main():
    p = argparse.ArgumentParser(description=__doc__)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--project", choices=list(projects()))
    selection.add_argument("--all", action="store_true")
    p.add_argument("--serve", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18000)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    telemetry = Telemetry()
    results = []
    for identifier in ([args.project] if args.project else list(projects()) if args.all else []):
        report = execute(identifier, telemetry)
        results.append(report)
        if not args.json:
            good = sum(c["passed"] for c in report["checks"])
            print(f"{identifier} {report['title']}: {good}/{len(report['checks'])} checks passed ({report['duration_seconds']}s)", flush=True)
    telemetry.flush()
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    if args.serve:
        from shared.server import serve
        serve(args.host, args.port)
    telemetry.shutdown()
    return int(any(not c["passed"] for r in results for c in r["checks"]))


if __name__ == "__main__":
    raise SystemExit(main())
