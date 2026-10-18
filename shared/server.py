"""Read-only HTML evidence viewer and Prometheus exposition of actual run results."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime
from shared.runner import ROOT, projects, reports

STYLE = """
:root{--bg:#0b1422;--card:#142236;--line:#293e55;--text:#e6edf5;--muted:#9fb1c6;--green:#68e0b7;--amber:#ffc78a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}a{color:var(--green);text-decoration:none}main{max-width:1400px;margin:auto;padding:28px 44px 45px}.eyebrow{font-size:11px;letter-spacing:2px;color:var(--green);text-transform:uppercase}h1{font-size:36px;letter-spacing:-1px;line-height:1.1;margin:9px 0 12px}h2{font-size:17px;margin:0 0 15px}p{color:var(--muted);margin:8px 0 18px}.topline{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:24px}.badge{font-size:12px;background:#173d37;border:1px solid #27725d;color:var(--green);padding:7px 12px;border-radius:20px;white-space:nowrap}.bad{color:#ff9d9d;background:#45272b;border-color:#733}.grid{display:grid;grid-template-columns:1.08fr 1fr;gap:20px}.card{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:22px}.story{padding:0;overflow:hidden}.story img{width:100%;display:block;aspect-ratio:1.5;object-fit:cover}.caption{padding:13px 20px;font-size:12px;color:var(--muted)}.checks{display:grid;gap:9px}.check{display:grid;grid-template-columns:22px 1fr;gap:5px;border-bottom:1px solid var(--line);padding:0 0 9px}.check:last-child{border:0;padding:0}.tick{color:var(--green);font-size:17px}.check strong{font-size:13px}.detail{display:block;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.measurements{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:20px 0}.measure{padding:15px;background:#142236;border:1px solid var(--line);border-radius:12px}.measure b{display:block;font-size:22px;color:var(--amber);overflow-wrap:anywhere}.measure small{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.6px}.timeline{display:flex;gap:12px;flex-wrap:wrap}.step{flex:1;min-width:170px;border-left:2px solid #68e0b7;padding:0 12px}.step b{font-size:12px}.step span{font-size:11px;display:block;color:var(--muted)}.footer{margin-top:18px;font-size:10px;color:var(--muted)}.indexgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.indexgrid .card{display:grid;grid-template-columns:100px 1fr;gap:17px}.indexgrid img{width:100px;border-radius:8px}.indexgrid h2{margin-bottom:3px}.indexgrid p{font-size:12px;margin:0}.tags{display:flex;gap:8px}.tag{border:1px solid var(--line);border-radius:4px;padding:3px 6px;font-size:10px;color:var(--muted)}@media(max-width:850px){main{padding:22px}.grid,.indexgrid{grid-template-columns:1fr}.measurements{grid-template-columns:repeat(2,1fr)}h1{font-size:28px}}
"""


def shell(title, body):
    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>'


def render(report):
    identifier = report["id"]
    passing = sum(bool(c["passed"]) for c in report["checks"])
    checks = "".join(f'<div class="check"><span class="tick">{"✓" if c["passed"] else "✕"}</span><div><strong>{escape(c["name"])}</strong><span class="detail">{escape(str(c.get("detail", "")))}</span></div></div>' for c in report["checks"])
    values = [(k, v) for k, v in report.get("measurements", {}).items() if isinstance(v, (int, float, str))][:9]
    measurements = "".join(f'<div class="measure"><b>{escape(str(round(v, 4) if isinstance(v, float) else v))}</b><small>{escape(k.replace("_", " "))}</small></div>' for k, v in values)
    steps = "".join(f'<div class="step"><b>{escape(str(t.get("phase", "")))}</b><span>{escape(str(t.get("detail", "")))}</span></div>' for t in report.get("timeline", [])[:5])
    return shell(report["title"], f'''
<div class="topline"><a href="/" class="eyebrow">OBSERVABILITY PRACTICE / PROJECT {identifier}</a><span class="badge {'bad' if passing != len(report['checks']) else ''}">{passing} / {len(report['checks'])} SCENARIO CHECKS PASSED</span></div>
<h1>{escape(report['title'])}</h1><p>{escape(report.get('summary', ''))}</p>
<div class="grid"><div class="card story"><img src="/assets/{identifier}/architecture.webp" alt="Illustrated architecture"><div class="caption">Architecture story · See README for exact data paths and failure boundaries</div></div><div class="card"><h2>Evidence from the latest execution</h2><div class="checks">{checks}</div></div></div>
<div class="measurements">{measurements}</div>
<div class="card"><h2>How the scenario unfolds</h2><div class="timeline">{steps}</div></div>
<div class="footer">ACTUAL PYTHON EXECUTION • {escape(report.get('executed_at',''))} • RUN {escape(report.get('run_id',''))} • {report.get('duration_seconds',0)} seconds · <a href="/api/reports/{identifier}">Raw JSON evidence</a> · Fluent Bit / OpenTelemetry / Grafana available through Docker Compose</div>''')


def metrics():
    lines = ["# HELP prep_check_result Latest scenario result, 1 pass 0 fail", "# TYPE prep_check_result gauge",
             "# HELP prep_project_duration_seconds Measured project duration", "# TYPE prep_project_duration_seconds gauge"]
    for r in reports():
        lines.append(f'prep_project_duration_seconds{{project="{r["id"]}"}} {r["duration_seconds"]}')
        lines.append(f'prep_project_last_run_timestamp_seconds{{project="{r["id"]}"}} {datetime.fromisoformat(r["executed_at"]).timestamp()}')
        for i, c in enumerate(r["checks"]):
            lines.append(f'prep_check_result{{project="{r["id"]}",check="{i:02d}"}} {int(c["passed"])}')
        for name, value in r.get("measurements", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                label = json.dumps(name)
                lines.append(f'prep_measurement{{project="{r["id"]}",measurement={label}}} {value}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send(200, "application/json", json.dumps({"status": "ready", "projects": len(reports())}))
        if path == "/metrics":
            return self.send(200, "text/plain; version=0.0.4", metrics())
        if path == "/api/reports":
            return self.send(200, "application/json", json.dumps(reports(), default=str))
        if path.startswith("/assets/"):
            parts = path.split("/")
            if len(parts) == 4 and parts[2] in projects() and parts[3] in ("architecture.webp", "demo.jpg"):
                file = projects()[parts[2]] / "assets" / parts[3]
                if file.exists():
                    return self.send(200, "image/webp" if file.suffix == ".webp" else "image/jpeg", file.read_bytes())
        if path.startswith("/project/") or path.startswith("/api/reports/"):
            identifier = path.rsplit("/", 1)[-1]
            report = next((r for r in reports() if r["id"] == identifier), None)
            if report:
                if path.startswith("/api/"):
                    return self.send(200, "application/json", json.dumps(report, default=str))
                return self.send(200, "text/html", render(report))
        if path == "/":
            cards = "".join(f'<a class="card" href="/project/{r["id"]}"><img src="/assets/{r["id"]}/architecture.webp"><div><span class="eyebrow">PROJECT {r["id"]}</span><h2>{escape(r["title"])}</h2><p>{sum(c["passed"] for c in r["checks"])} scenario checks passed · {r["duration_seconds"]} seconds</p></div></a>' for r in reports())
            return self.send(200, "text/html", shell("Observability Practice Projects", '<div class="topline"><span class="eyebrow">PYTHON · FLUENT BIT · OPENTELEMETRY · GRAFANA</span><span class="badge">10 PERSONAL LEARNING PROJECTS</span></div><h1>Learn by investigating failures.</h1><p>Runnable projects, measured experiments, architectural decisions, and verified observability evidence.</p><div class="indexgrid">' + cards + '</div><div class="footer">Each page reads real execution reports. Architecture illustrations are conceptual; component configurations are in infra/.</div>'))
        self.send(404, "text/plain", "Not found")

    def send(self, code, content_type, body):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host="127.0.0.1", port=8000):
    print(f"Evidence viewer: http://{host}:{port}", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
