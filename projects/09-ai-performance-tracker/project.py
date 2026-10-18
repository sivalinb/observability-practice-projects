"""A real, small CPU language model served through streaming HTTP."""
from __future__ import annotations

import concurrent.futures
import json
import math
import random
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

ID = "09"
TITLE = "AI Performance Tracker"


class TinyLanguageModel:
    """Autoregressive trigram language model with explicit CPU probability scoring."""
    def __init__(self, corpus, scoring_rounds=80):
        words = re.findall(r"[a-z]+|[.!?]", corpus.lower())
        self.vocabulary = sorted(set(words))
        self.unigrams = Counter(words)
        self.contexts = defaultdict(Counter)
        for index in range(2, len(words)):
            self.contexts[tuple(words[index - 2:index])][words[index]] += 1
        self.scoring_rounds = scoring_rounds
        self.corpus_tokens = len(words)

    def prefill(self, prompt):
        context = re.findall(r"[a-z]+|[.!?]", prompt.lower())
        # Compute context likelihood rather than sleeping to manufacture latency.
        score = 0.0
        for _ in range(self.scoring_rounds):
            for index in range(2, len(context)):
                counts = self.contexts.get(tuple(context[index - 2:index]), {})
                score += math.log((counts.get(context[index], 0) + .1) / (sum(counts.values()) + .1 * len(self.vocabulary)))
        return (context or ["the", "service"])[-2:], score

    def next_token(self, context, rng):
        counts = self.contexts.get(tuple(context[-2:]), {})
        weights = []
        # Repeated probability scoring gives a measurable CPU workload. It is
        # declared explicitly; it is not a neural decoder or GPU approximation.
        for _ in range(self.scoring_rounds):
            weights = [counts.get(word, 0) + .01 * self.unigrams[word] for word in self.vocabulary]
            denominator = sum(weights)
            weights = [value / denominator for value in weights]
        token = rng.choices(self.vocabulary, weights=weights, k=1)[0]
        context.append(token)
        return token


class Runtime:
    def __init__(self, workers=2, scoring_rounds=80):
        corpus = Path(__file__).with_name("corpus.txt").read_text()
        self.model = TinyLanguageModel(corpus, scoring_rounds)
        self.workers = threading.BoundedSemaphore(workers)
        self.lock = threading.Lock()
        self.records = []
        self.active = 0
        self.max_active = 0
        self.worker_limit = workers
        self.provider = TracerProvider(resource=Resource.create({"service.name": "tiny-inference", "inference.model": "trigram-cpu"}))
        self.span_exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.span_exporter))
        import os
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        self.tracer = self.provider.get_tracer("ai-performance-tracker")


def handler_for(runtime):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            with runtime.lock:
                records = list(runtime.records)
                active = runtime.active
            metrics = ["# TYPE inference_requests_total counter", "inference_requests_total %d" % len(records),
                       "# TYPE inference_generated_tokens_total counter", "inference_generated_tokens_total %d" % sum(row["tokens"] for row in records),
                       "# TYPE inference_active_workers gauge", "inference_active_workers %d" % active,
                       "# TYPE inference_queue_seconds_sum counter", "inference_queue_seconds_sum %.9f" % sum(row["queue_seconds"] for row in records),
                       "# TYPE inference_ttft_seconds_sum counter", "inference_ttft_seconds_sum %.9f" % sum(row["ttft_seconds"] for row in records)]
            data = ("\n".join(metrics) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/generate":
                self.send_error(404)
                return
            arrived = time.perf_counter()
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 100000:
                    raise ValueError("body must be 1..100000 bytes")
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise ValueError("body must be a JSON object")
                prompt = request.get("prompt", "the service")
                if not isinstance(prompt, str) or len(prompt) > 20000:
                    raise ValueError("prompt must be a string under 20000 characters")
                count = request.get("max_tokens", 24)
                if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 128:
                    raise ValueError("max_tokens must be 1..128")
                seed = request.get("seed", 7)
                if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32 - 1:
                    raise ValueError("seed must be an unsigned 32-bit integer")
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_error(400, str(error))
                return
            request_span = runtime.tracer.start_span("POST /generate", kind=SpanKind.SERVER,
                attributes={"http.request.method": "POST", "http.route": "/generate", "inference.max_tokens": count})
            parent_context = trace.set_span_in_context(request_span)
            queue_span = runtime.tracer.start_span("inference.queue", context=parent_context)
            acquired = runtime.workers.acquire(timeout=15)
            queue_span.end()
            if not acquired:
                request_span.set_status(Status(StatusCode.ERROR, "inference queue timeout"))
                request_span.end()
                self.send_error(503, "inference queue timeout")
                return
            entered = time.perf_counter()
            with runtime.lock:
                runtime.active += 1
                runtime.max_active = max(runtime.max_active, runtime.active)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()
                with runtime.tracer.start_as_current_span("inference.prefill", context=parent_context):
                    context, likelihood = runtime.model.prefill(prompt)
                rng = random.Random(seed)
                first = None
                with runtime.tracer.start_as_current_span("inference.decode", context=parent_context):
                    for index in range(count):
                        token = runtime.model.next_token(context, rng)
                        now = time.perf_counter()
                        if first is None:
                            first = now
                        self.wfile.write((json.dumps({"token": token, "index": index}) + "\n").encode())
                        self.wfile.flush()
                finished = time.perf_counter()
                record = {"tokens": count, "prompt_tokens": len(re.findall(r"[a-z]+|[.!?]", prompt.lower())),
                          "queue_seconds": entered - arrived, "ttft_seconds": first - arrived,
                          "tpot_seconds": (finished - first) / max(1, count - 1),
                          "generation_seconds": finished - entered, "total_seconds": finished - arrived,
                          "context_log_likelihood": round(likelihood, 3)}
                with runtime.lock:
                    runtime.records.append(record)
                request_span.set_attributes({"inference.output_tokens": count, "inference.queue_seconds": record["queue_seconds"],
                                             "inference.ttft_seconds": record["ttft_seconds"], "inference.tpot_seconds": record["tpot_seconds"]})
                self.wfile.write((json.dumps({"done": True, "timing": record}) + "\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                request_span.set_status(Status(StatusCode.ERROR, "client disconnected"))
            finally:
                with runtime.lock:
                    runtime.active -= 1
                runtime.workers.release()
                request_span.end()
                self.close_connection = True
    return Handler


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bound accepted handler threads as well as active model workers."""
    daemon_threads = True

    def __init__(self, address, handler, ingress_limit=16):
        self.ingress = threading.BoundedSemaphore(ingress_limit)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        return request, address

    def process_request(self, request, client_address):
        if not self.ingress.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.ingress.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.ingress.release()


@contextmanager
def serving(workers=2, scoring_rounds=80, ingress_limit=16):
    runtime = Runtime(workers, scoring_rounds)
    server = BoundedHTTPServer(("127.0.0.1", 0), handler_for(runtime), ingress_limit)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, runtime
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        runtime.provider.force_flush()
        runtime.provider.shutdown()


def generate(endpoint, prompt, max_tokens=24, seed=7):
    started = time.perf_counter()
    request = urllib.request.Request(endpoint + "/generate", json.dumps({"prompt": prompt, "max_tokens": max_tokens, "seed": seed}).encode(), {"Content-Type": "application/json"})
    token_times = []
    pieces = []
    final = None
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            event = json.loads(line)
            if "token" in event:
                token_times.append(time.perf_counter())
                pieces.append(event["token"])
            if event.get("done"):
                final = event["timing"]
    finished = time.perf_counter()
    if final is None or not token_times:
        raise RuntimeError("Incomplete token stream")
    return {"output": " ".join(pieces), "tokens": len(pieces), "server": final,
            "client_ttft_seconds": token_times[0] - started,
            "client_tpot_seconds": (token_times[-1] - token_times[0]) / max(1, len(pieces) - 1),
            "client_total_seconds": finished - started}


def ollama_generate(endpoint, model, prompt, max_tokens=24):
    """Optional real LLM streaming comparison; never fabricated when unavailable."""
    started = time.perf_counter()
    request = urllib.request.Request(endpoint.rstrip("/") + "/api/generate",
        json.dumps({"model": model, "prompt": prompt, "stream": True, "options": {"num_predict": max_tokens, "temperature": 0}}).encode(),
        {"Content-Type": "application/json"})
    first = None
    output = []
    final = None
    with urllib.request.urlopen(request, timeout=180) as response:
        for line in response:
            event = json.loads(line)
            if event.get("response"):
                first = first or time.perf_counter()
                output.append(event["response"])
            if event.get("done"):
                final = event
    if first is None or final is None:
        raise RuntimeError("Ollama did not return a complete stream")
    return {"model": model, "provider": "ollama", "output": "".join(output), "client_ttft_seconds": first - started,
            "client_total_seconds": time.perf_counter() - started, "eval_count": final.get("eval_count"),
            "eval_duration_ns": final.get("eval_duration"), "prompt_eval_duration_ns": final.get("prompt_eval_duration")}


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    cases = {}
    with serving() as (endpoint, runtime):
        cases["short-prompt"] = generate(endpoint, "the checkout service", 20)
        cases["long-prompt"] = generate(endpoint, "the checkout service " * 120, 20)
        burst_started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(generate, endpoint, "the checkout service " * 20, 24, seed) for seed in range(8)]
            burst = [future.result() for future in futures]
        burst_seconds = time.perf_counter() - burst_started
        cases["concurrent-burst"] = burst
        metrics = urllib.request.urlopen(endpoint + "/metrics").read().decode()
        (output_dir / "inference.prom").write_text(metrics)
        active_max = runtime.max_active
        corpus_tokens = runtime.model.corpus_tokens
        vocabulary = len(runtime.model.vocabulary)
    spans = [{"name": span.name, "trace_id": format(span.context.trace_id, "032x"),
              "span_id": format(span.context.span_id, "016x"), "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
              "duration_ns": span.end_time - span.start_time, "attributes": dict(span.attributes)} for span in runtime.span_exporter.get_finished_spans()]
    (output_dir / "inference-spans.json").write_text(json.dumps(spans, indent=2))
    import os
    if os.environ.get("INFERENCE_OLLAMA_MODEL"):
        cases["ollama-comparison"] = ollama_generate(os.environ.get("INFERENCE_OLLAMA_ENDPOINT", "http://127.0.0.1:21434"), os.environ["INFERENCE_OLLAMA_MODEL"], "Explain telemetry reliability in one sentence.")
    (output_dir / "request-results.json").write_text(json.dumps(cases, indent=2))
    queue_times = [row["server"]["queue_seconds"] for row in burst]
    throughput = sum(row["tokens"] for row in burst) / burst_seconds
    checks = [
        {"name": "HTTP delivers a complete incremental token stream", "passed": cases["short-prompt"]["tokens"] == 20 and all(row["tokens"] == 24 for row in burst), "detail": "NDJSON tokens are read incrementally before the final timing event."},
        {"name": "Model is trained from the checked-in corpus", "passed": corpus_tokens > 100 and vocabulary > 20, "detail": "%d corpus tokens / %d word vocabulary" % (corpus_tokens, vocabulary)},
        {"name": "Worker limit holds under concurrent requests", "passed": active_max <= 2, "detail": "Observed maximum active inference workers: %d" % active_max},
        {"name": "Queue time and inference time are separately measured", "passed": max(queue_times) > 0 and all(row["server"]["generation_seconds"] > 0 for row in burst), "detail": "Maximum queue wait %.4f seconds; no artificial sleeps." % max(queue_times)},
        {"name": "Metrics expose completed requests and tokens", "passed": "inference_requests_total 10" in metrics and "inference_generated_tokens_total 232" in metrics, "detail": "Prometheus exposition fetched from the running HTTP service."},
        {"name": "Client and server observe nonzero first-token delay", "passed": cases["short-prompt"]["client_ttft_seconds"] > 0 and cases["short-prompt"]["server"]["ttft_seconds"] > 0, "detail": "Client timing includes HTTP transport; server timing starts at handler arrival."},
        {"name": "OpenTelemetry separates queue, prefill and decode spans", "passed": len(spans) == 40 and {row["name"] for row in spans} == {"POST /generate", "inference.queue", "inference.prefill", "inference.decode"}, "detail": "%d actual SDK spans recorded for ten completed requests." % len(spans)},
    ]
    return {"id": ID, "title": TITLE, "summary": "Serve a tiny CPU language model and measure queue delay, first token, token spacing, and useful throughput.", "checks": checks,
            "measurements": {"requests": 10, "generated_tokens": 232, "max_active_workers": active_max,
                             "burst_tokens_per_second": round(throughput, 3), "max_queue_seconds": round(max(queue_times), 6),
                             "short_prompt_ttft_seconds": round(cases["short-prompt"]["client_ttft_seconds"], 6),
                             "long_prompt_ttft_seconds": round(cases["long-prompt"]["client_ttft_seconds"], 6),
                             "mean_burst_tpot_seconds": round(statistics.mean(row["client_tpot_seconds"] for row in burst), 6),
                             "execution_seconds": round(time.perf_counter() - started, 4)},
            "timeline": [{"phase": "Request arrives", "detail": "Prompt length and output budget enter a real streaming HTTP endpoint."},
                         {"phase": "Queue", "detail": "Eight clients share two bounded CPU inference workers."},
                         {"phase": "First token", "detail": "Trigram context likelihood is computed and an output token is sampled."},
                         {"phase": "Token stream", "detail": "The client measures time between received output tokens."},
                         {"phase": "Capacity review", "detail": "Compare queue, TTFT, TPOT and output throughput; do not infer GPU behavior."}],
            "scenarios": ["Short prompt", "Long prompt", "Sequential requests", "Eight concurrent clients", "Two-worker limit", "Streaming token delivery", "Client/server timing difference", "Prometheus metrics", "Optional Ollama real LLM comparison"]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/09"))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.serve:
        if not 1 <= args.workers <= 16:
            parser.error("--workers must be 1..16")
        runtime = Runtime(workers=args.workers)
        server = BoundedHTTPServer(("127.0.0.1", args.port), handler_for(runtime))
        print("Tiny CPU language model: http://127.0.0.1:%d/generate" % server.server_port, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            runtime.provider.force_flush()
            runtime.provider.shutdown()
    else:
        print(json.dumps(run_demo(args.output), indent=2))
