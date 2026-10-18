"""Ordered Kafka -> Loki delivery, with manual offsets and a persistent local journal.

Loki HTTP and SQLite cannot commit atomically: a crash after Loki acceptance but
before the local transaction may duplicate a log. Event IDs permit detection.
This is deliberately at-least-once, not an exactly-once claim.
"""
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, urlopen


class DeliveryJournal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS delivered(event_id TEXT PRIMARY KEY, topic TEXT, partition_id INTEGER, offset_id INTEGER)")
        self.db.commit()

    def contains(self, event_id):
        return self.db.execute("SELECT 1 FROM delivered WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark(self, event_id, topic, partition, offset):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO delivered VALUES(?,?,?,?)", (event_id, topic, partition, offset))


def push_loki(record, base_url):
    payload = {"streams": [{"stream": {"job": "observability-practice", "source": "kafka", "project": str(record.get("project", "01"))},
                             "values": [[str(time.time_ns()), json.dumps(record, sort_keys=True)]]}]}
    request = Request(base_url.rstrip("/") + "/loki/api/v1/push", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        if response.status != 204:
            raise RuntimeError(f"Loki did not acknowledge: {response.status}")


def main():
    from kafka import KafkaConsumer, TopicPartition, OffsetAndMetadata
    logging.basicConfig(level=logging.INFO)
    journal = DeliveryJournal(os.getenv("BRIDGE_DB", "data/bridge/offsets.db"))
    consumer = KafkaConsumer("prep-events", bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092").split(","),
                             group_id="prep-loki-bridge", enable_auto_commit=False, auto_offset_reset="earliest", max_poll_records=50)
    try:
        while True:
            batches = consumer.poll(timeout_ms=1000)
            for partition, records in batches.items():
                for message in records:
                    try:
                        record = json.loads(message.value)
                        event_id = record["event_id"]
                        if not journal.contains(event_id):
                            push_loki(record, os.getenv("LOKI_URL", "http://localhost:3100"))
                            journal.mark(event_id, message.topic, message.partition, message.offset)
                        consumer.commit({partition: OffsetAndMetadata(message.offset + 1, "", getattr(message, "leader_epoch", -1))})
                    except Exception:
                        logging.exception("Delivery unacknowledged; replaying offset %s", message.offset)
                        consumer.seek(partition, message.offset)
                        time.sleep(1)
                        break
    finally:
        consumer.close()
        journal.db.close()


if __name__ == "__main__":
    main()
