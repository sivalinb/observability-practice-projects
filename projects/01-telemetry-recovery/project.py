"""A real append/fsync WAL, crash recovery, HTTP replay and idempotent sink.

Only the Python standard library is needed for the deterministic local demo.
The shared deployment adds OpenTelemetry, Fluent Bit and Grafana.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class WALCorruption(ValueError):
    pass


class CapacityExceeded(BufferError):
    pass


def canonical(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def sync_directory(path: Path) -> None:
    """Persist renames where the OS exposes directory fsync."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DurableWAL:
    """Single writer, sequential acknowledgements, bounded retained bytes.

    An append is accepted only after fsync. An incomplete final record is
    removed on recovery. A complete record with a bad checksum fails closed.
    This deliberately does not claim multi-writer, replicated durability.
    """

    def __init__(self, path: Path, max_bytes: int = 1_000_000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint = self.path.with_suffix(".checkpoint.json")
        self.max_bytes = max_bytes
        self.truncated_bytes = 0
        self.records: list[dict] = []
        created = not self.path.exists()
        self.path.touch(exist_ok=True)
        if created:
            sync_directory(self.path.parent)
        raw = self.path.read_bytes()
        complete_end = raw.rfind(b"\n") + 1
        if complete_end < len(raw):
            self.truncated_bytes = len(raw) - complete_end
            with self.path.open("r+b") as stream:
                stream.truncate(complete_end)
                stream.flush()
                os.fsync(stream.fileno())
        for index, line in enumerate(raw[:complete_end].splitlines(), 1):
            try:
                record = json.loads(line)
                payload = record["payload"]
                expected = hashlib.sha256(canonical(payload)).hexdigest()
                if record["checksum"] != expected:
                    raise ValueError("checksum mismatch")
            except (ValueError, KeyError, TypeError) as error:
                raise WALCorruption(f"complete record {index}: {error}") from error
            self.records.append(payload)
        self.acked = 0
        if self.checkpoint.exists():
            self.acked = int(json.loads(self.checkpoint.read_text())["acked"])
        if not 0 <= self.acked <= len(self.records):
            raise WALCorruption("checkpoint outside durable record range")

    def append(self, payload: dict) -> int:
        record = {"payload": payload, "checksum": hashlib.sha256(canonical(payload)).hexdigest()}
        encoded = canonical(record) + b"\n"
        if self.path.stat().st_size + len(encoded) > self.max_bytes:
            raise CapacityExceeded("retained WAL bytes reached the configured bound")
        with self.path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self.records.append(payload)
        return len(self.records)

    def pending(self):
        yield from enumerate(self.records[self.acked :], self.acked + 1)

    def acknowledge(self, position: int) -> None:
        if position != self.acked + 1 or position > len(self.records):
            raise ValueError("acknowledgements must advance exactly one durable record")
        temporary = self.checkpoint.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump({"acked": position}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.checkpoint)
        sync_directory(self.path.parent)
        self.acked = position


class DeduplicatingSink:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as database:
            database.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            database.execute("CREATE TABLE IF NOT EXISTS attempts (event_id TEXT NOT NULL)")

    def connect(self):
        database = sqlite3.connect(self.path)
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def accept(self, payload: dict) -> bool:
        with self.connect() as database:
            database.execute("INSERT INTO attempts VALUES (?)", (payload["event_id"],))
            cursor = database.execute("INSERT OR IGNORE INTO events VALUES (?, ?)", (payload["event_id"], json.dumps(payload)))
            return cursor.rowcount == 1

    def counts(self) -> tuple[int, int]:
        with self.connect() as database:
            return (database.execute("SELECT count(*) FROM events").fetchone()[0],
                    database.execute("SELECT count(*) FROM attempts").fetchone()[0])


def receiver(sink: DeduplicatingSink):
    state = {"available": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if not state["available"]:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"backend unavailable")
                return
            try:
                inserted = sink.accept(json.loads(body))
            except (ValueError, KeyError):
                self.send_error(400)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"inserted": inserted}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


def post(url: str, payload: dict) -> bool:
    request = Request(url, canonical(payload), {"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=2) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError):
        return False


def replay(wal: DurableWAL, url: str) -> int:
    delivered = 0
    for position, payload in wal.pending():
        if not post(url, payload):
            break
        wal.acknowledge(position)
        delivered += 1
    return delivered


def run_demo(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = Path(tempfile.mkdtemp(prefix="recovery-", dir=output_dir))
    wal = DurableWAL(state_dir / "events.jsonl")
    ingress: queue.Queue = queue.Queue(maxsize=4)
    for sequence in range(4):
        ingress.put_nowait({"event_id": f"event-{sequence:03d}", "sequence": sequence, "value": sequence * 2})
    queue_rejected = False
    try:
        ingress.put_nowait({"event_id": "overflow"})
    except queue.Full:
        queue_rejected = True
    while not ingress.empty():
        wal.append(ingress.get_nowait())
    for sequence in range(4, 24):
        wal.append({"event_id": f"event-{sequence:03d}", "sequence": sequence, "value": sequence * 2})
    sink = DeduplicatingSink(state_dir / "sink.sqlite")
    server, thread, backend = receiver(sink)
    url = f"http://127.0.0.1:{server.server_port}/events"
    try:
        outage_delivered = replay(wal, url)
        outage_acked = wal.acked
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--crash-append", str(wal.path)], capture_output=True, text=True, timeout=10)
        with wal.path.open("ab") as stream:
            stream.write(b'{"interrupted_tail":')
            stream.flush()
            os.fsync(stream.fileno())
        recovered = DurableWAL(wal.path)
        recovered_count = len(recovered.records)
        backend["available"] = True
        # This is the unavoidable ambiguous window: sink commit precedes ACK.
        first = next(recovered.pending())[1]
        accepted_without_checkpoint = post(url, first)
        restarted = DurableWAL(wal.path)
        started = time.perf_counter()
        replayed = replay(restarted, url)
        replay_seconds = time.perf_counter() - started
        unique, attempts = sink.counts()
        checkpoint_after_restart = DurableWAL(wal.path).acked
        disk_rejected = False
        tiny = DurableWAL(state_dir / "finite.jsonl", max_bytes=256)
        tiny.append({"event_id": "small"})
        try:
            tiny.append({"event_id": "oversized", "body": "x" * 1024})
        except CapacityExceeded:
            disk_rejected = True
        corrupt = state_dir / "corrupt.jsonl"
        corrupt.write_bytes(canonical({"payload": {"event_id": "x"}, "checksum": "bad"}) + b"\n")
        corrupt_rejected = False
        try:
            DurableWAL(corrupt)
        except WALCorruption:
            corrupt_rejected = True
        expected = 25
        checks = [
            {"name": "Bounded ingress rejects overload", "passed": queue_rejected, "detail": "Fifth item rejected by a four-slot queue before durable acceptance."},
            {"name": "HTTP outage retains every accepted event", "passed": outage_delivered == 0 and outage_acked == 0, "detail": f"503 response: delivered={outage_delivered}, checkpoint={outage_acked}."},
            {"name": "fsync survives abrupt process exit", "passed": child.returncode == 27 and recovered_count == expected, "detail": f"Child exited {child.returncode}; recovered {recovered_count} events."},
            {"name": "Incomplete tail recovered", "passed": recovered.truncated_bytes > 0 and recovered_count == expected, "detail": f"Removed {recovered.truncated_bytes} incomplete bytes; all complete records retained."},
            {"name": "At-least-once replay deduplicated", "passed": accepted_without_checkpoint and replayed == expected and unique == expected and attempts == expected + 1, "detail": f"{attempts} committed delivery attempts produced {unique} unique event IDs."},
            {"name": "Checkpoint survives reopen", "passed": checkpoint_after_restart == expected, "detail": f"Durable checkpoint={checkpoint_after_restart}."},
            {"name": "Finite disk capacity rejects before acceptance", "passed": disk_rejected and len(DurableWAL(tiny.path).records) == 1, "detail": "Configured 256-byte bound rejected an oversized record without changing accepted history."},
            {"name": "Complete checksum corruption fails closed", "passed": corrupt_rejected, "detail": "Corrupt complete record requires operator investigation; no silent skip."},
        ]
        return {"id": "01", "title": "Telemetry Recovery", "summary": "Durable Python WAL recovered 25 events across a real HTTP outage and abrupt writer exit; one replay was deduplicated.", "checks": checks,
                "measurements": {"accepted_events": expected, "sink_unique_events": unique, "delivery_attempts": attempts, "duplicates_suppressed": attempts - unique, "wal_bytes": wal.path.stat().st_size, "recovery_seconds": round(replay_seconds, 4), "replay_events_per_second": round(expected / max(replay_seconds, 1e-9), 1), "incomplete_tail_bytes": recovered.truncated_bytes, "ingress_capacity": ingress.maxsize},
                "timeline": [{"phase": "Durable acceptance", "detail": "24 events appended and fsynced while a four-slot ingress demonstrates overload rejection."}, {"phase": "Backend outage", "detail": "An actual local HTTP receiver returns 503; no WAL position advances."}, {"phase": "Writer crash", "detail": "A child process fsyncs event 25 then exits abruptly; recovery trims an incomplete tail."}, {"phase": "Ambiguous acknowledgement", "detail": "The sink commits one event before a simulated client restart; its replay is deduplicated."}, {"phase": "Recovery", "detail": f"{unique} unique events recovered in {replay_seconds:.3f}s; sequential checkpoints persisted."}],
                "scenarios": ["HTTP 503 and retry retention", "bounded in-memory admission", "abrupt child-process exit after fsync", "incomplete trailing write", "complete checksum corruption", "sink commit before client checkpoint", "SQLite event-ID deduplication", "durable sequential checkpoint", "finite configured WAL capacity"],
                "artifacts": {"wal": str(wal.path), "sink": str(sink.path), "checkpoint": str(restarted.checkpoint)}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-append", type=Path)
    arguments = parser.parse_args()
    if arguments.crash_append:
        DurableWAL(arguments.crash_append).append({"event_id": "event-024", "sequence": 24, "value": 48})
        os._exit(27)
