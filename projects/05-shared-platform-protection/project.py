"""Authenticated, bounded, weighted HTTP query admission with tenant isolation."""
from __future__ import annotations

import json
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Policy:
    capacity: int = 20
    refill_per_second: float = 0
    concurrency: int = 2
    weight: int = 1
    max_query_bytes: int = 512
    queue_capacity: int = 32


@dataclass
class Job:
    tenant: str
    work_ms: float = 20
    done: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)


class WeightedScheduler:
    """Weighted round robin applies to dispatch opportunities, not CPU seconds."""
    def __init__(self, policies: dict[str, Policy]):
        if not policies or any(p.weight < 1 or p.concurrency < 1 or p.queue_capacity < 1
                               for p in policies.values()):
            raise ValueError("policies require positive weights and bounds")
        self.policies = policies
        self.queues = {tenant: deque() for tenant in policies}
        self.active = Counter()
        self.max_active = Counter()
        self.dispatches = Counter()
        self.order = [tenant for tenant, policy in policies.items() for _ in range(policy.weight)]
        self.cursor = 0
        self.closed = False
        self.cv = threading.Condition()

    def put(self, job: Job) -> bool:
        with self.cv:
            if self.closed or len(self.queues[job.tenant]) >= self.policies[job.tenant].queue_capacity:
                return False
            self.queues[job.tenant].append(job)
            self.cv.notify_all()
            return True

    def get(self, timeout: float = 2) -> Job | None:
        deadline = time.monotonic() + timeout
        with self.cv:
            while not self.closed:
                for _ in self.order:
                    tenant = self.order[self.cursor % len(self.order)]
                    self.cursor += 1
                    if self.queues[tenant] and self.active[tenant] < self.policies[tenant].concurrency:
                        self.active[tenant] += 1
                        self.max_active[tenant] = max(self.max_active[tenant], self.active[tenant])
                        self.dispatches[tenant] += 1
                        return self.queues[tenant].popleft()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cv.wait(remaining)
        return None

    def complete(self, job: Job):
        with self.cv:
            self.active[job.tenant] -= 1
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


class Gateway:
    def __init__(self, policies: dict[str, Policy] | None = None, workers: int = 4):
        self.policies = policies or {"alpha": Policy(capacity=8, weight=2),
                                     "beta": Policy(), "gamma": Policy()}
        self.tokens = {f"demo-{tenant}-token": tenant for tenant in self.policies}
        self.buckets = {tenant: [float(policy.capacity), time.monotonic()]
                        for tenant, policy in self.policies.items()}
        self.scheduler = WeightedScheduler(self.policies)
        self.counts = Counter()
        self.latencies = {tenant: [] for tenant in self.policies}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(workers)]
        for worker in self.workers:
            worker.start()

    def authenticate(self, authorization: str) -> str | None:
        return self.tokens.get(authorization.removeprefix("Bearer ")) if authorization.startswith("Bearer ") else None

    def admit(self, tenant: str) -> bool:
        policy = self.policies[tenant]
        with self.lock:
            tokens, timestamp = self.buckets[tenant]
            now = time.monotonic()
            tokens = min(policy.capacity, tokens + (now - timestamp) * policy.refill_per_second)
            allowed = tokens >= 1
            self.buckets[tenant] = [tokens - int(allowed), now]
            self.counts[(tenant, "admitted" if allowed else "rate_limited")] += 1
            return allowed

    def _worker(self):
        while not self.stop.is_set():
            job = self.scheduler.get(timeout=.1)
            if job is None:
                continue
            try:
                time.sleep(job.work_ms / 1000)
                job.result = {"tenant": job.tenant, "rows": 3, "checksum": sum(range(1000))}
            finally:
                self.scheduler.complete(job)
                job.done.set()

    def close(self):
        self.stop.set()
        self.scheduler.close()
        for worker in self.workers:
            worker.join(timeout=2)


def handler_for(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            start = time.monotonic()
            tenant = gateway.authenticate(self.headers.get("Authorization", ""))
            if tenant is None:
                self.respond(401, {"error": "valid bearer token required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.respond(400, {"error": "invalid length"})
                return
            if length > 2048 or length < 0:
                self.respond(413, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                query = body.get("query", "")
                if not isinstance(query, str):
                    raise ValueError("query must be text")
                if len(query.encode()) > gateway.policies[tenant].max_query_bytes:
                    self.respond(413, {"error": "query too large"})
                    return
                work_ms = float(body.get("work_ms", 20))
                if not 0 <= work_ms <= 200:
                    raise ValueError("work bound exceeded")
            except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
                self.respond(400, {"error": "invalid query request"})
                return
            if not gateway.admit(tenant):
                self.respond(429, {"error": "tenant rate quota exceeded", "tenant": tenant})
                return
            job = Job(tenant, work_ms)
            if not gateway.scheduler.put(job):
                self.respond(429, {"error": "tenant queue is full", "tenant": tenant})
                return
            if not job.done.wait(timeout=5):
                self.respond(504, {"error": "query deadline exceeded"})
                return
            with gateway.lock:
                gateway.latencies[tenant].append((time.monotonic() - start) * 1000)
                gateway.counts[(tenant, "completed")] += 1
            self.respond(200, job.result)
    return Handler


def request_query(port: int, token: str, query: str = "count errors", forged_tenant: str = "", work_ms: float = 20):
    payload = json.dumps({"query": query, "work_ms": work_ms}).encode()
    request = Request(f"http://127.0.0.1:{port}/query", data=payload,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                               "X-Tenant": forged_tenant})
    try:
        with urlopen(request, timeout=8) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    gateway = Gateway()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(gateway))
    server.request_queue_size = 128
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        unauthorized = request_query(server.server_port, "invalid")[0]
        oversized = request_query(server.server_port, "demo-beta-token", query="x" * 513)[0]
        forged = request_query(server.server_port, "demo-alpha-token", forged_tenant="beta")
        tenants = ["alpha"] * 40 + ["beta", "gamma"] * 6
        # Interleave healthy work with the noisy tenant rather than serializing tenants.
        tenants = ["alpha", "beta", "gamma"] * 6 + ["alpha"] * 34
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [(tenant, pool.submit(request_query, server.server_port, f"demo-{tenant}-token"))
                       for tenant in tenants]
            outcomes = [(tenant, *future.result()) for tenant, future in futures]
        observed = {tenant: Counter(status for name, status, _ in outcomes if name == tenant)
                    for tenant in gateway.policies}
        # A continuously backlogged dispatch experiment makes weighted service measurable.
        scheduler = WeightedScheduler({"alpha": Policy(weight=2, queue_capacity=64),
                                       "beta": Policy(weight=1, queue_capacity=64)})
        for tenant in scheduler.policies:
            for _ in range(60):
                scheduler.put(Job(tenant, 0))
        order = []
        for _ in range(30):
            job = scheduler.get()
            order.append(job.tenant)
            scheduler.complete(job)
        fairness = Counter(order)
        bounded = WeightedScheduler({"alpha": Policy(queue_capacity=1)})
        queue_bound = bounded.put(Job("alpha")) and not bounded.put(Job("alpha"))
        checks = [
            {"name": "unauthenticated_request", "passed": unauthorized == 401, "detail": f"HTTP {unauthorized}"},
            {"name": "trusted_tenant_identity", "passed": forged[0] == 200 and forged[1]["tenant"] == "alpha", "detail": f"forged X-Tenant=beta resolved to {forged[1].get('tenant')}"},
            {"name": "oversized_query", "passed": oversized == 413, "detail": f"513-byte query returned HTTP {oversized}"},
            {"name": "noisy_tenant_quota", "passed": observed["alpha"][429] >= 30 and observed["alpha"][200] <= 7, "detail": str(dict(observed["alpha"]))},
            {"name": "healthy_tenants_isolated", "passed": observed["beta"][200] == 6 and observed["gamma"][200] == 6, "detail": f"beta {dict(observed['beta'])}; gamma {dict(observed['gamma'])}"},
            {"name": "query_concurrency_bounded", "passed": all(gateway.scheduler.max_active[t] <= p.concurrency for t, p in gateway.policies.items()), "detail": str(dict(gateway.scheduler.max_active))},
            {"name": "weighted_dispatch_fairness", "passed": fairness["alpha"] == 20 and fairness["beta"] == 10, "detail": f"first 30 backlogged dispatches: {dict(fairness)}"},
            {"name": "bounded_per_tenant_queue", "passed": queue_bound, "detail": "Second pending job rejected at capacity one"},
        ]
        (output_dir / "gateway-results.json").write_text(json.dumps({"requests": [{"tenant": t, "status": s, "body": b} for t, s, b in outcomes], "weighted_order": order, "max_active": dict(gateway.scheduler.max_active)}, indent=2))
        measurements = {"requests": len(outcomes) + 3, "noisy_rejections": observed["alpha"][429],
                        "healthy_completions": observed["beta"][200] + observed["gamma"][200],
                        "weighted_alpha_dispatches": fairness["alpha"], "weighted_beta_dispatches": fairness["beta"],
                        "max_per_tenant_concurrency": max(gateway.scheduler.max_active.values()),
                        "healthy_max_latency_ms": round(max(gateway.latencies["beta"] + gateway.latencies["gamma"]), 2)}
        return {"id": "05", "title": "Shared Platform Protection",
                "summary": "Authenticated tenant identity, quotas, bounded queues, concurrency limits, and weighted dispatch protect shared query capacity.",
                "checks": checks, "measurements": measurements,
                "timeline": [{"phase": "Identify", "detail": "Resolve fixture bearer tokens to trusted tenant identities."},
                             {"phase": "Admit", "detail": "Bound request bytes, tokens, and pending work per tenant."},
                             {"phase": "Schedule", "detail": "Dispatch with weights and separate active-query limits; measure healthy tenant completion."}],
                "scenarios": [c["name"] for c in checks]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway.close()
