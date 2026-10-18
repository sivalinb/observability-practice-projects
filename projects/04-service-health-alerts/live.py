"""Optional wall-clock SLI source for exercising Prometheus rules over real windows."""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from project import execute_batch


def metrics_text(snapshot):
    return (f'# TYPE prep_sli_requests_total counter\nprep_sli_requests_total{{project="04"}} {snapshot["total"]}\n'
            f'# TYPE prep_sli_errors_total counter\nprep_sli_errors_total{{project="04"}} {snapshot["bad"]}\n'
            f'# TYPE prep_sli_last_sample_timestamp_seconds gauge\nprep_sli_last_sample_timestamp_seconds{{project="04"}} {snapshot["last_sample"]}\n')


def serve(port=8084, initial_bad=0, host="127.0.0.1"):
    if not isinstance(initial_bad, int) or not 0 <= initial_bad <= 1000:
        raise ValueError("initial_bad must be an integer between 0 and 1000")
    state = {"total": 0, "bad": 0, "last_sample": 0, "injected_bad": initial_bad}
    lock, stop = threading.Lock(), threading.Event()

    def workload():
        while not stop.is_set():
            with lock:
                injected = state["injected_bad"]
            total, bad, _ = execute_batch(1000, injected)
            with lock:
                state["total"] += total
                state["bad"] += bad
                state["last_sample"] = time.time()
            stop.wait(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urlsplit(self.path)
            status = 200
            if path.path == "/metrics":
                with lock:
                    snapshot = dict(state)
                body = metrics_text(snapshot).encode()
                content_type = "text/plain; version=0.0.4"
            elif path.path == "/scenario":
                try:
                    bad = int(parse_qs(path.query).get("bad", ["0"])[0])
                    if not 0 <= bad <= 1000:
                        raise ValueError()
                    with lock:
                        state["injected_bad"] = bad
                    body = json.dumps({"bad_per_1000": bad}).encode()
                except ValueError:
                    status, body = 400, b'{"error":"bad must be between 0 and 1000"}'
                content_type = "application/json"
            else:
                status, body, content_type = 404, b"not found", "text/plain"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    worker = threading.Thread(target=workload, daemon=True)
    worker.start()
    print(f"SLI metrics: http://{host}:{server.server_port}/metrics", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bad-per-batch", type=int, choices=range(1001), default=0, metavar="0..1000")
    args = parser.parse_args()
    serve(args.port, args.bad_per_batch, args.host)
