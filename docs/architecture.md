# Architecture and durability

The ten projects run real Python implementations against controlled synthetic inputs. Their common telemetry stack lets you inspect each experiment's measurements and evidence in Grafana. A project report, a live service SLI, a delivery acknowledgement and durable storage are different contracts; this document names each boundary explicitly.

The illustrated architecture images in each project README tell the failure story. The exact deployed wiring comes from [compose.yaml](../compose.yaml), [Collector configuration](../infra/otel.yaml), [Fluent Bit configuration](../infra/fluent-bit.conf), [Prometheus configuration](../infra/prometheus.yml), and the [shared Python runner](../shared/runner.py). The [scenario inventory](scenarios.md) maps assertions to tests; [recorded verification](verification.md) identifies what actually ran.

## Follow one execution through the system

Run `python run.py --project 01` to execute a project, or `python run.py --all --serve` to execute all projects and serve the evidence viewer. The runner imports each project's `run_demo(output_dir)` and gives it a fresh artifact directory. That function performs the experiment and returns checks, numeric/string measurements, a timeline and scenario names.

The runner saves `artifacts/<project>/<run-id>/report.json`, updates `artifacts/<project>/latest.json`, and appends one JSON outcome record per check to `data/logs/app.jsonl`. An exception from a project's execution becomes an explicit failed report, replacing a previous green snapshot. A failed check produces a nonzero CLI exit. The viewer reads these actual saved reports; its screenshots are captures of execution evidence rather than drawn interface mockups.

Three telemetry paths leave the runner:

| Signal | Exact route in Compose | Read surface |
| --- | --- | --- |
| Numeric experiment results | `run_demo → latest.json → prep:8000/metrics → Prometheus scrape every 5s` | Grafana Prometheus source; `prep_measurement`, `prep_check_result`, execution time and last-run timestamp |
| SDK traces and check counter | Python OpenTelemetry SDK → OTLP HTTP `otel:4318` → Collector `memory_limiter` → persistent exporter queue → `tempo:4318` for traces or `prometheus:9090/api/v1/otlp/v1/metrics` for metrics | Grafana Tempo source; Prometheus `prep_check_runs_total` |
| JSON scenario evidence | Runner fsynced `app.jsonl` → shared named volume → Fluent Bit tail/parser → filesystem chunks → Loki HTTP at `loki:3100` | Grafana Loki source; `{job="observability-practice",project="01"}` |

These are separate paths. The `prep_measurement` values come from saved project results exposed for scraping; they are not a renamed copy of the SDK counter. The SDK counter measures executed checks. Traces help inspect project/check execution. The logs retain scenario names, decisions, detail and run IDs. If one path is unavailable, another path's success does not prove it delivered.

OpenTelemetry export is enabled when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Compose sets it to `http://otel:4318`. For a host Python process sending to this Compose deployment, use `http://127.0.0.1:14318`. A local run without that variable still generates reports, outcome logs and scrape metrics but does not enable the runner's OTLP export.

## Separate experiment results from continuous service health

`prep_check_result{project="01",check="00"}` is a gauge describing a check in the **latest experiment**. Its check label is an index; use the report or scenario log for the name. `prep_measurement{project="01",measurement="sink_unique_events"}` is the result recorded by that experiment. Repeated scrapes repeat the latest saved value; they do not rerun the experiment or establish that the application is currently healthy.

Always inspect freshness with:

```promql
time() - prep_project_last_run_timestamp_seconds{project="01"}
```

For failing checks in a completed experiment:

```promql
prep_check_result{project="01"} == 0
```

For a latest-result overview:

```promql
sum by (project) (prep_check_result)
count by (project) (prep_check_result)
```

The continuous source is project 04's separate `live-sli` service. It executes batches of 1,000 operations on a wall-clock loop and exposes `prep_sli_requests_total`, `prep_sli_errors_total` and `prep_sli_last_sample_timestamp_seconds`. Prometheus scrapes `live-sli:8084` and evaluates the supplied recording/alert rules. Change the injected error count at [the scenario endpoint](http://localhost:18084/scenario?bad=20), then observe the real counter rates and alert state. Reset with `bad=0`.

The Python project 04 demo compresses six hours of explicit event timestamps into a short test. The continuous source uses real elapsed time. Prometheus can evaluate a range with partial startup history; a full six-hour wall-clock observation still requires that history. Python's exact event sums and Prometheus's extrapolated counter rates need not have identical boundary values. [Prometheus query semantics](https://prometheus.io/docs/prometheus/latest/querying/basics/) explain why a range query and an instant value answer different questions.

## Verify the right measurements in Grafana for every project

Open [Grafana](http://localhost:13000/d/prep-overview), choose the project's provisioned dashboard, and inspect the **Actual project measurements** table. These panels query Prometheus directly with `prep_measurement{project="XX"}`. The same page shows check results and Loki scenario evidence. Open **Explore → Prometheus**, paste one of the exact expressions below, and use an instant query for a latest experiment result. [Grafana Explore](https://grafana.com/docs/grafana/latest/visualizations/explore/) provides the underlying query interface.

| Project and dashboard | Exact measurement queries to inspect | Interpretation and evidence |
| --- | --- | --- |
| [01 · Telemetry Recovery](http://localhost:13000/d/prep01) | `prep_measurement{project="01",measurement="sink_unique_events"}`<br>`prep_measurement{project="01",measurement="delivery_attempts"}`<br>`prep_measurement{project="01",measurement="duplicates_suppressed"}`<br>`prep_measurement{project="01",measurement="wal_bytes"}` | Compare unique events with successful delivery attempts and suppressed duplicates. Inspect the saved WAL/checkpoint and sink for durability evidence. [Screenshot](../projects/01-telemetry-recovery/assets/demo.jpg) |
| [02 · Metric Quality Checker](http://localhost:13000/d/prep02) | `prep_measurement{project="02",measurement="before_violations"}`<br>`prep_measurement{project="02",measurement="after_violations"}`<br>`prep_measurement{project="02",measurement="histogram_series_before"}`<br>`prep_measurement{project="02",measurement="histogram_series_after"}`<br>`prep_measurement{project="02",measurement="histogram_declared_upper_bound"}` | Distinguish observed histogram series before/after repair from the declared bounded-vocabulary maximum. [Screenshot](../projects/02-metric-quality-checker/assets/demo.jpg) |
| [03 · Telemetry Accuracy Checker](http://localhost:13000/d/prep03) | `prep_measurement{project="03",measurement="http_ingest_accepts"}`<br>`prep_measurement{project="03",measurement="persisted_samples"}`<br>`prep_measurement{project="03",measurement="faults_detected"}`<br>`prep_measurement{project="03",measurement="stale_oldest_seconds"}` | Acceptance and storage counts deliberately differ in the dropped-sample fault. These are local canary experiment results, not a claim of current Prometheus completeness. [Screenshot](../projects/03-telemetry-accuracy-checker/assets/demo.jpg) |
| [04 · Service Health Alerts](http://localhost:13000/d/prep04) | `prep_measurement{project="04",measurement="http_batches"}`<br>`prep_measurement{project="04",measurement="executed_operations"}`<br>`prep_measurement{project="04",measurement="fast_burn_rate"}`<br>`prep_measurement{project="04",measurement="slow_burn_rate"}` | These values describe accelerated logical-window fixtures. Use `rate(prep_sli_errors_total{project="04"}[5m]) / rate(prep_sli_requests_total{project="04"}[5m])` and `prep:sli_burn_rate:5m` for the separate live source. [Screenshot](../projects/04-service-health-alerts/assets/demo.jpg) |
| [05 · Shared Platform Protection](http://localhost:13000/d/prep05) | `prep_measurement{project="05",measurement="noisy_rejections"}`<br>`prep_measurement{project="05",measurement="healthy_completions"}`<br>`prep_measurement{project="05",measurement="healthy_max_latency_ms"}`<br>`prep_measurement{project="05",measurement="weighted_alpha_dispatches"}`<br>`prep_measurement{project="05",measurement="weighted_beta_dispatches"}` | Check healthy completion during a noisy burst and the separate continuously backlogged dispatch experiment. Dispatch weight does not establish CPU-time fairness. [Screenshot](../projects/05-shared-platform-protection/assets/demo.jpg) |
| [06 · Performance Troubleshooter](http://localhost:13000/d/prep06) | `prep_measurement{project="06",measurement="trace_count"}`<br>`prep_measurement{project="06",measurement="span_count"}`<br>`prep_measurement{project="06",measurement="head_retained"}`<br>`prep_measurement{project="06",measurement="tail_retained"}`<br>`prep_measurement{project="06",measurement="instrumentation_delta_us"}` | Numeric results compare offline retention policies and measured overhead. Follow saved trace IDs in Tempo when OTLP is enabled; read `.pstats` for the separate CPU profile. [Screenshot](../projects/06-performance-troubleshooter/assets/demo.jpg) |
| [07 · Telemetry Cost Optimizer](http://localhost:13000/d/prep07) | `prep_measurement{project="07",measurement="baseline_gzip_bytes"}`<br>`prep_measurement{project="07",measurement="aggregate_gzip_bytes"}`<br>`prep_measurement{project="07",measurement="mean_shard_p95_ms"}`<br>`prep_measurement{project="07",measurement="correct_combined_p95_ms"}` | Compare actual fixture byte sizes and unsafe quantile averaging. The pricing projection is an explicit model in the artifact, not a vendor bill. [Screenshot](../projects/07-telemetry-cost-optimizer/assets/demo.jpg) |
| [08 · AI Incident Investigator](http://localhost:13000/d/prep08) | `prep_measurement{project="08",measurement="source_records"}`<br>`prep_measurement{project="08",measurement="retrieved_records"}`<br>`prep_measurement{project="08",measurement="supported_citations"}`<br>`prep_measurement{project="08",measurement="abstention_cases"}` | Read the report's string `model_mode` and optional `model-investigation.json` to distinguish the deterministic baseline from an actual model call. String fields are not exported as numeric measurements. [Screenshot](../projects/08-ai-incident-investigator/assets/demo.jpg) |
| [09 · AI Performance Tracker](http://localhost:13000/d/prep09) | `prep_measurement{project="09",measurement="burst_tokens_per_second"}`<br>`prep_measurement{project="09",measurement="max_queue_seconds"}`<br>`prep_measurement{project="09",measurement="short_prompt_ttft_seconds"}`<br>`prep_measurement{project="09",measurement="long_prompt_ttft_seconds"}`<br>`prep_measurement{project="09",measurement="mean_burst_tpot_seconds"}` | These measured timings belong to the CPU trigram service and that run's machine. They do not characterize a transformer or GPU. Inspect `inference-spans.json` and the optional model-comparison artifact separately. [Screenshot](../projects/09-ai-performance-tracker/assets/demo.jpg) |
| [10 · Service Monitoring Starter Kit](http://localhost:13000/d/prep10) | `prep_measurement{project="10",measurement="generated_files"}`<br>`prep_measurement{project="10",measurement="rejected_invalid_contracts"}`<br>`prep_measurement{project="10",measurement="exported_spans"}`<br>`prep_measurement{project="10",measurement="json_log_records"}`<br>`prep_measurement{project="10",measurement="metric_bytes"}` | The demo launches and tests a generated service. Its saved service metrics/spans/logs are separate from the root runner's measurements. Deploy the generated scrape/dashboard/log assets explicitly for a continuously running service. [Screenshot](../projects/10-service-monitoring-starter-kit/assets/demo.jpg) |

For each project, query Loki with its bounded project label and inspect `passed`, `detail` and `run_id`:

```logql
{job="observability-practice",project="01"} | json
```

Runner outcome logs do not carry the SDK span trace ID; join their evidence by project, run ID and time. Project 10's generated request logs do carry actual trace IDs, which its local assertions compare with HTTP responses and exported spans. The core Fluent Bit instance tails the runner's `app.jsonl`; it does not automatically tail every generated service artifact.

## The durability boundary table

“WAL” names a write-ahead logging mechanism, not one universal guarantee. These mechanisms protect different state at different moments:

| Boundary | Implemented persistence and acknowledgement | What survives / what remains outside the guarantee |
| --- | --- | --- |
| Project 01 volatile ingress | Four-slot Python queue; overload rejected before durable admission | A queued-but-unappended event can disappear on process exit. Rejection is observable and is not counted as accepted data. |
| Project 01 delivery WAL | Append payload + checksum + newline; flush and `os.fsync`; only then return success | Demonstrated child-process exit recovery. Incomplete final fragment is removed; complete checksum corruption fails closed. Configured retained-byte bound rejects new writes. No compaction, multiwriter coordination, replicated storage or actual `ENOSPC` injection. |
| Project 01 sender checkpoint | Sequential position written to temporary file, fsynced, atomically replaced, parent directory fsynced | Reopening resumes at the durable acknowledgement. Sink commit before checkpoint can cause replay. |
| Python SQLite sink / project 03 canary store | SQLite `journal_mode=WAL`, `synchronous=FULL`; commit transaction before the normal successful response | Sink event ID has a unique constraint, so a repeated delivery does not repeat the insert. Project 03 intentionally violates normal acceptance in its drop fault. Local transactional durability does not replicate storage. |
| Python SDK span batches | `BatchSpanProcessor` queue in process memory, plus explicit flush/shutdown on normal completion | A crash before export can lose ended spans. This is not the project 01 append/fsync boundary. |
| Collector outbound queues | `file_storage` on `otel-wal`, `fsync: true`; both exporters use `storage: file_storage`, `queue_size: 1000`, retry enabled with `max_elapsed_time: 0s` | Persisted queue work can replay after Collector process restart while the volume remains. Queue units are export requests, not a guaranteed byte budget or 1,000 events. Finite disk, enqueue rejection, memory limiting, malformed data and permanent backend rejection remain relevant. |
| Fluent Bit tail metadata | SQLite tail DB on `fluent-storage`; `DB.Sync Full`; SQLite WAL tracks file offsets | A cursor transaction records reading progress. It does not by itself preserve the corresponding unsent log payload. |
| Fluent Bit payload chunks | `storage.type filesystem`, `storage.sync full`, checksums, persistent chunk directory; output retry unlimited | Filesystem chunks protect buffered payloads across tested process restarts. The output's `storage.total_limit_size 64M` can discard the oldest chunks when its limit is reached; unlimited retry does not imply unlimited retention. |
| Kafka replay profile | Fluent Bit filesystem queue → idempotent Kafka producer with `acks=all` → one persistent broker → Python consumer with auto-commit disabled | Broker retention permits independent consumer replay. This deployment has one broker and replication factor one; there is no separate replica to preserve data after its host/storage loss. |
| Kafka-to-Loki bridge journal | Loki success → SQLite WAL/FULL delivery mark → manual Kafka offset commit | A restart after the local mark can skip a duplicate push. A crash after Loki accepts but before the SQLite mark can duplicate the log. Loki, SQLite and Kafka do not share a transaction. |

SQLite's transaction WAL and checkpoint behavior are described in [SQLite WAL documentation](https://www.sqlite.org/wal.html). Collector storage settings follow the [pinned file-storage extension documentation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.139.0/extension/storage/filestorage/README.md). Fluent Bit distinguishes its [tail database](https://docs.fluentbit.io/manual/data-pipeline/inputs/tail) from [filesystem buffering and output limits](https://docs.fluentbit.io/manual/data-pipeline/buffering).

The Collector configuration deliberately has **no batch processor** before its persistent exporter queues. Accepted export work can reach `file_storage` without first waiting in a volatile Collector batch. `memory_limiter` remains before the exporters and can refuse work under pressure; transport/retry policy must handle that refusal. This design improves one failure boundary while preserving finite capacity and backend acceptance constraints. The separate Python SDK batch buffer remains volatile. See the official [Collector resilience guidance](https://opentelemetry.io/docs/collector/resiliency/) and [Python SDK span processor API](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html).

None of these settings establishes zero loss under every failure, end-to-end exactly-once delivery or survival of host/storage destruction. The demonstrations prove named process/outage/replay scenarios with retained local volumes. A production claim needs a failure model, replicated storage/placement decisions, retention limits, capacity reservations and independent reconciliation.

## Kafka is optional and has a specific job

The default JSON outcome path goes directly through Fluent Bit to Loki. The `replay` profile adds a separate path for `data/logs/kafka.jsonl`: Fluent Bit → `prep-events` → the `prep-loki-bridge` consumer group → Loki with `source="kafka"`. It does not put synchronous request handling or individual inference tokens behind Kafka.

The producer uses the event ID as its message key, `acks=-1`, and producer idempotence. The Python bridge processes records in partition order, pushes to Loki, commits a local event-ID delivery mark, then manually commits **the next Kafka offset** including its leader epoch. On error it seeks back to the failed offset and retries; a poison record can hold that partition until corrected. The journal currently retains delivery IDs indefinitely and needs a retention/compaction policy for long-lived use.

The acknowledgement ambiguity remains between systems. A successful Loki push with a missing local mark is replayable, so duplication is permitted. A surviving mark with an uncommitted Kafka offset avoids a repeated push on the next delivery. Event IDs make this inspectable; they do not create a distributed transaction. [Kafka's delivery-semantics design](https://kafka.apache.org/39/design/design/#message-delivery-semantics) explains why processing guarantees depend on coordinating the destination with consumer position. The one-broker practice topology offers no independent failure-domain replication.

## Project-specific data remains inspectable

Projects 01 and 03 create their own temporary local HTTP pipelines with durable evidence stores. They do not impersonate Prometheus/Loki ingestion in their algorithm tests. Project 02 validates logical metric snapshots and calculates classic histogram series expansion; it is not a full scrape parser. Compatible cumulative histograms may be merged before estimating quantiles; per-shard p95 values cannot be averaged into a global p95. See [Prometheus histogram guidance](https://prometheus.io/docs/practices/histograms/).

Project 06 gives each run a UUID-derived trace prefix while preserving controlled low bits for its head-selection example, preventing separate runs from merging under fixed trace IDs. Its completed-trace retention comparison is offline and does not configure Collector tail sampling. The CPU profile runs the same hotspot function in an isolated Python subprocess and writes `.pstats`; it is a separate measured workload, not a continuously correlated production profiler. Isolation avoids the observed CPython 3.12 profiler-monitoring interaction with active SDK/HTTP threads.

Project 09 records real SDK request/queue/prefill/decode spans and optionally exports them when OTLP is configured. Its temporary standalone `/metrics` endpoint ends with the experiment; the shared dashboard reads the saved experiment measurements. Project 10 deliberately clears the inherited remote OTLP target during local subprocess verification and saves generated-service SDK spans to JSONL. To run that generated service continuously, use its README, set the desired OTLP destination and install its scrape/dashboard/Fluent Bit assets. Its current Fluent Bit output targets Loki, matching the supplied stack.

## Ports, storage and operational ownership

All published service ports bind to `127.0.0.1`. Container-to-container traffic uses service names and internal ports; do not replace those with host ports inside Compose.

| Surface | Host address | Internal destination |
| --- | --- | --- |
| Grafana | [localhost:13000](http://localhost:13000) | `grafana:3000` |
| Evidence viewer | [localhost:18000](http://localhost:18000) | `prep:8000` |
| Prometheus | [localhost:19090](http://localhost:19090) | `prometheus:9090` |
| Loki HTTP | [localhost:13100](http://localhost:13100/ready) | `loki:3100` |
| Tempo HTTP | [localhost:13200](http://localhost:13200/ready) | `tempo:3200`; trace ingestion uses `tempo:4318` |
| Collector OTLP HTTP | `http://localhost:14318` | `otel:4318` |
| Collector health | [localhost:13333](http://localhost:13333) | `otel:13133` |
| Fluent Bit diagnostics | [localhost:12020](http://localhost:12020) | `fluent-bit:2020` |
| Continuous SLI source | [localhost:18084/metrics](http://localhost:18084/metrics) | `live-sli:8084` |
| Optional Kafka | `localhost:29092` | `kafka:9092`; external advertised listener `localhost:29092` |
| Optional Ollama | `http://localhost:21434` | `ollama:11434` |

Named volumes keep reports/logs, Collector queues, Fluent Bit cursor/chunks, Prometheus data, Tempo WAL/blocks, Loki data, Kafka data and Grafana state across container restarts. They remain on the local container runtime's storage; `down --volumes` or runtime/disk loss can remove them. Prometheus retains two days, and Tempo's configured trace retention is 48 hours. These local settings are chosen for practice cost and inspection, not a production retention or high-availability policy.

When reviewing a design, define the accepted-data boundary first, then derive outage storage and catch-up capacity. Explain which component owns each retry and which downstream acceptance it observes. Use the report, exact backend query and failure-injection evidence to support the claim; keep untested disk-loss, scaling and regional guarantees outside it.
