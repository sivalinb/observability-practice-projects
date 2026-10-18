# Scenario inventory and evidence

Each project has executable code, a saved report and explicit assertions. This inventory records what those assertions mean and where their scope ends. Test counts are intentionally not duplicated here; run the current suite and read the current reports for exact results.

From the repository root:

```bash
python -m unittest discover -s tests -v
python run.py --all
```

Run one project with `python run.py --project 01`. Its latest report is `artifacts/01/latest.json`; `artifact_path` identifies the detailed run directory. The command exits nonzero for failed checks. An execution exception becomes a failed latest report instead of leaving the previous result green. The [architecture guide](architecture.md) maps every project's numeric evidence to exact Grafana queries, explains the durability boundaries and distinguishes result snapshots from continuous SLIs.

## 01 · Telemetry Recovery

[Implementation and reasoning](../projects/01-telemetry-recovery/) · [Tests](../tests/test_01_recovery.py) · [Executed Grafana screenshot](../projects/01-telemetry-recovery/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Bounded ingress | The fifth insertion into a four-slot queue raises queue-full before durable acceptance. |
| Backend outage | A real local HTTP receiver returns 503; replay completes no deliveries and advances no checkpoint. |
| Abrupt writer exit | A child appends and fsyncs its event, exits with `os._exit`, and the reopened WAL contains it. |
| Interrupted final write | Recovery removes an incomplete trailing fragment while keeping all complete records. |
| Corrupt complete record | Checksum validation raises `WALCorruption`; a complete corrupt record is not silently skipped. |
| Commit/ack ambiguity | The sink commits one event without a sender checkpoint; replay yields an extra successful delivery attempt but no extra unique sink row. |
| Durable acknowledgement | Reopening preserves the final checkpoint; acknowledging record two before record one is rejected. |
| Finite configured capacity | An oversized append fails before acceptance and does not grow the accepted record history. |

Inspect the report's WAL, checkpoint and SQLite artifact paths. Capacity rejection is a configured byte-limit test, not injected filesystem `ENOSPC`. This exercise tests process exit and a truncated-write fixture, not power loss, host destruction, replicated storage, compaction or concurrent writers.

## 02 · Metric Quality Checker

[Implementation and reasoning](../projects/02-metric-quality-checker/) · [Tests](../tests/test_02_contracts.py) · [Executed Grafana screenshot](../projects/02-metric-quality-checker/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Unsafe before fixture | Enforce mode rejects versioned-contract violations; warn mode admits while preserving the same diagnostics. |
| Repaired fixture | Approved names, owner, unit, labels and histogram boundaries produce zero violations. |
| User identity cardinality | Forbidden user-ID labels create distinct observed combinations and exceed the series budget. |
| Classic histogram expansion | Four finite boundaries plus `+Inf`, sum and count yield seven wire series per label set. |
| Observed versus declared bound | The corrected observed count is below the declared combinatorial bound; lowering the contract budget catches the declared bound even while observations fit. |
| Stable repeated observations | Repeating the same label set does not add a time series. Duplicate metric descriptors are rejected. |
| Schema retirement | The legacy metric is accepted immediately before its removal date and rejected on the date. |
| Invalid values | Nonfinite observations and negative counters fail validation. |

Inspect `before-validation.json`, `warn-validation.json` and `after-validation.json`. The snapshots describe logical observations; this is not a full OpenMetrics/native-histogram parser or live ingestion firewall. Cardinality across a supplied snapshot does not measure time-window churn or prove that a partial inventory is complete.

## 03 · Telemetry Accuracy Checker

[Implementation and reasoning](../projects/03-telemetry-accuracy-checker/) · [Tests](../tests/test_03_accuracy.py) · [Executed Grafana screenshot](../projects/03-telemetry-accuracy-checker/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Healthy canary | Six HTTP-ingested samples match independently known sequences, exact values, labels and freshness. |
| Acknowledged drop | The injected collector accepts but omits sequence three; the HTTP query result exposes the gap. |
| Stale timestamps | Age exceeds the configured freshness budget and is reported independently from process health. |
| Wrong labels / values | A rewritten region and changed arithmetic value produce separate integrity failures. |
| Query failure | A real SQL failure becomes an unavailable HTTP query and a failed canary result. |
| Missing producer | No emission yields empty results and a dead-man failure, even if the process endpoint is healthy. |
| Future time / duplicate sequence | Future timestamps and duplicate sequence IDs fail direct invariant tests. |
| Green process, bad data | Every process health endpoint remains successful while all injected data faults are detected. |

Inspect `canary-outcomes.json` and the SQLite artifact. These are actual HTTP-to-SQLite tests with an independent expected ledger, not a claim that the same sequence history was tested through Prometheus scraping. The optional Prometheus command proves query API success only. Continuous external checking, late-arrival grace and independent alert delivery are outside this implementation.

## 04 · Service Health Alerts

[Implementation and reasoning](../projects/04-service-health-alerts/) · [Python tests](../tests/test_04_alerts.py) · [Prometheus rules](../projects/04-service-health-alerts/prometheus-rules.yml) · [Promtool fixtures](../projects/04-service-health-alerts/prometheus-rules.test.yml) · [Executed Grafana screenshot](../projects/04-service-health-alerts/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Brief spike | Short-window burn is high but the long-window threshold does not fire. |
| Sustained failure | Both fast windows exceed the fast burn threshold. |
| Slow degradation | Both slow windows exceed the slower burn threshold. |
| Recovery | Clearing the applicable short windows stops the page even with retained long-window history. |
| No traffic versus no data | A fresh zero-operation sample is `no_traffic`; absent/stale observations are `missing_data`. |
| Future timestamp | Python excludes it; Prometheus suppresses burn paging and raises telemetry failure. |
| Actual operation counts | HTTP batches execute operations and return their counted successes/failures; batch count and operation count are asserted separately. |
| Live source / rule path | Live-source input bounds and metric text are tested; the stack scrapes its separate wall-clock counters. Promtool verifies valid rules and timestamped burn/missing-data fixtures. |

Inspect `window-evaluations.json` and `events.json`. Logical event time compresses the six-hour Python scenario; it is not a real-time soak. Promtool's historical fixtures are another logical-time evaluation. Live rates require the wall-clock `live-sli` service and sufficient scrape history. Notification routing, Alertmanager delivery and a latency SLO are outside these availability checks.

## 05 · Shared Platform Protection

[Implementation and reasoning](../projects/05-shared-platform-protection/) · [Tests](../tests/test_05_isolation.py) · [Executed Grafana screenshot](../projects/05-shared-platform-protection/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Authentication / identity | Invalid token gets 401; an alpha token with forged `X-Tenant: beta` still runs as alpha. |
| Query and work bounds | Oversized query gets 413; fixture work above its bound gets 400 before dispatch. |
| Noisy tenant | Alpha exhausts its own token budget and gets 429 while beta and gamma complete their admitted work. |
| Active-query isolation | Per-tenant active counts never exceed configured concurrency. |
| Weighted dispatch | A separate experiment keeps queues continuously backlogged and verifies the declared 2:1 dispatch ratio. |
| Queue exhaustion | A capacity-one pending queue rejects its second waiting job; a blocked tenant does not prevent another runnable tenant from dispatching. |

Inspect `gateway-results.json` for actual statuses, identities, dispatch order and active counts. The demo's token refill is zero to make burst exhaustion reproducible. Weighted dispatch is not weighted CPU time; the workload is controlled read-only work rather than an actual query engine. Scheduler bounds do not bound HTTP connection threads or establish distributed quotas across replicas.

## 06 · Performance Troubleshooter

[Implementation and reasoning](../projects/06-performance-troubleshooter/) · [Tests](../tests/test_06_performance.py) · [Executed Grafana screenshot](../projects/06-performance-troubleshooter/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Cross-service context | Real frontend/dependency HTTP requests retain trace IDs and correct parent span relationships. |
| Rare error | An injected 503 trace lies outside the fixture head set but is retained by the completed-trace policy. |
| Slow dependency | A deliberate dependency wait is retained by the latency policy. |
| CPU attribution | An isolated Python process profiles five actual hotspot calls; assertions inspect `.pstats` call counts and ranked output. |
| Instrumentation cost | Alternating baseline/traced rounds produce measured median costs; no universal positive or maximum-overhead claim is made. |
| Invalid context / capacity | Malformed/all-zero trace context is rejected; exceeding the distinct-trace comparison bound raises an explicit error. |

Inspect `traces.json`, `cpu-profile.pstats`, `cpu-profile.txt` and `overhead.json`. Each execution gets a UUID-derived trace prefix with controlled low bits for reproducible sampling choices. The profiler subprocess is separate from the HTTP trace workload and avoids the observed CPython 3.12 monitoring interaction with active SDK/HTTP threads. This is an offline completed-trace comparison, not live Collector tail sampling or continuous trace-correlated profiling.

## 07 · Telemetry Cost Optimizer

[Implementation and reasoning](../projects/07-telemetry-cost-optimizer/) · [Tests](../tests/test_07_cost.py) · [Executed Grafana screenshot](../projects/07-telemetry-cost-optimizer/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Known incident fixture | Reproducible request records contain sustained errors, slow dependency observations and a rare failure. |
| Policy transformations | Full, uniform sample, incident-aware detail, one-minute and five-minute aggregation write actual compressed artifacts. |
| Diagnostic coverage | The same diagnosis rules compare retained representations with full-fixture incident truth. Minute aggregation preserves the fixture diagnoses; uniform sampling misses the chosen rare event. |
| Biased sampling | Sampled incident exemplars are not used as rate denominators; independent unsampled metrics are retained and counted in the byte total. |
| Histogram merge | Compatible cumulative bucket counts merge exactly; raw combined quantile differs from the unsafe average of shard p95 values. |
| Actual bytes / explicit model | Gzip sizes are measured, while the 30-day dollar projection uses disclosed synthetic pricing and constant-rate assumptions. |

Inspect `cost-and-diagnosis.json`, policy `.jsonl.gz` files and the independent unsampled metrics. These request records are synthetic fixtures, not captured production traffic. Preserving this small incident set does not prove all future incidents remain diagnosable. Byte reduction is not measured backend CPU, object-store cost, query speed or a vendor invoice.

## 08 · AI Incident Investigator

[Implementation and reasoning](../projects/08-ai-incident-investigator/) · [Tests](../tests/test_08_investigator.py) · [Executed Grafana screenshot](../projects/08-ai-incident-investigator/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Known incident | Tenant/time-scoped lexical retrieval finds structured supporting evidence for the known dependency hypothesis. |
| Missing / contradictory evidence | The explicit sufficiency policy abstains rather than forcing a conclusion. |
| Tenant isolation | South-tenant evidence is excluded from a north investigation. |
| Embedded log instruction | Malicious free text cannot change the deterministic baseline's structured evidence scoring. |
| Provenance and action gate | Invented IDs, unsupported conclusion, unsafe action and wrong-sign citations are rejected. |
| Competing explanations | Duplicate hypothesis names are rejected; alternatives must be distinct allowed names. |
| Duplicate source IDs | Duplicate evidence IDs fail instead of increasing apparent independent support. |
| Optional real model | When enabled, a real HTTP model result is validated before its artifact is saved; unavailable/invalid responses fail the run. |

Inspect `evidence.json`, `investigations.json`, `investigation.md` and, only when actually enabled successfully, `model-investigation.json`. The default is a deterministic evidence baseline with synthetic observation annotations. Source strings resemble Prometheus/Tempo/Fluent Bit locations; they are not live connector calls. The injection check establishes baseline behavior, not a general LLM prompt-injection guarantee. Scores are policy weights, not calibrated probabilities or causal proof.

## 09 · AI Performance Tracker

[Implementation and reasoning](../projects/09-ai-performance-tracker/) · [Tests](../tests/test_09_inference.py) · [Executed Grafana screenshot](../projects/09-ai-performance-tracker/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Incremental HTTP output | The client reads NDJSON token records and a final completion/timing event from actual CPU generation. |
| Short / long prompts | Both workloads produce real client/server first-token and token-spacing measurements. Timing ordering is not asserted as a universal scheduler guarantee. |
| Concurrency | Concurrent clients share a bounded worker semaphore; observed active work stays within its configured limit. |
| Ingress saturation | A held socket exhausts a capacity-one ingress test; a second raw connection observes the rejection response. |
| Malformed input | JSON lists, invalid seeds and invalid token counts receive 400 before successful streaming begins. |
| Repeatability | Identical prompt and seed produce the same token sequence. |
| Metrics / SDK tracing | The running endpoint exposes completed request/token counts; real SDK spans separate request, queue, prefill and decode. |
| Optional neural model | A configured Ollama endpoint can add actual streaming/provider timing evidence; it is not silently substituted for the default model. |

Inspect `request-results.json`, `inference.prom` and `inference-spans.json`. Admission occurs before HTTP parsing: a client still transmitting a rejected POST can observe a transport reset/broken pipe rather than a clean 503. The test intentionally reads the raw rejection boundary. Queue timing includes handler work before worker acquisition. The CPU trigram model, repeated scoring and Python GIL do not establish GPU/transformer capacity, KV-cache behavior or production quality.

## 10 · Service Monitoring Starter Kit

[Implementation and reasoning](../projects/10-service-monitoring-starter-kit/) · [Tests](../tests/test_10_starter.py) · [Executed Grafana screenshot](../projects/10-service-monitoring-starter-kit/assets/demo.jpg)

| Scenario | Actual assertion / evidence |
| --- | --- |
| Contract validation | Missing owner, forbidden labels, dynamic paths and invalid objectives are rejected before generating files. |
| Repeat generation | Identical contracts yield identical SHA-256 manifests; generated Python source compiles. |
| Working generated service | A real Python subprocess serves health/work routes and exposes Prometheus counters/histogram samples. |
| W3C parentage | Incoming trace and parent span IDs match the exported SDK server span. |
| Failure correlation | A real 500 has a matching metric/error span and JSON log with the same response trace ID. |
| Bounded unknown route | A customer-specific path returns 404 and only `__unmatched__` appears in metric labels. |
| Large latency objective | A generated 60-second target includes finite buckets above it; a 70-second observation remains distinguishable above the target. |
| Operational assets | Docker, Grafana, Prometheus and Fluent Bit files are generated and inspected; file presence is not a claim that every generated deployment was launched. |

Inspect `generated-service/`, its manifest, `service.prom`, `service-traces.jsonl`, `service.jsonl` and `responses.json`. The local verification clears inherited remote OTLP configuration, so generated-service traces are inspected locally. To deploy continuously, install the generated scrape/dashboard/log assets and set its exporter destination. The generated starter burn threshold is a simple onboarding rule; project 04 contains the multiwindow policy. Regeneration can overwrite service edits and needs an upgrade workflow in production.

## Shared stack verification

Run the actual configured stack before these checks:

```bash
python scripts/stack.py up --build -d
python scripts/verify_stack.py

# Optional broker replay and component restart scenarios:
python scripts/stack.py --replay up --build -d
python scripts/verify_stack.py --chaos --kafka
```

[Verification implementation](../scripts/verify_stack.py) · [Shared unit tests](../tests/test_shared.py) · [Recorded results](verification.md)

| Layer | Scope of the implemented verification |
| --- | --- |
| Project execution | Ten reports exist and all their returned scenario checks pass; a recorded execution failure prevents a green aggregate. |
| Scrape path | Prometheus's sum of actual `prep_check_result` values matches the current reports. |
| OTLP metric path | The SDK check counter reaches Prometheus through the Collector's OTLP exporter. |
| Continuous SLI path | The separate wall-clock source is queryable in Prometheus. |
| Log path | Known JSON canaries traverse the real Fluent Bit tail/filesystem/Loki route and are retrieved through Loki queries. |
| Trace path | Known SDK-generated trace IDs are retrievable from Tempo after actual OTLP export. |
| Grafana | Provisioned Prometheus, Loki and Tempo sources are inspected through their supported readiness/query routes; the ten project dashboard UIDs exist. |
| Alert rules | `promtool check rules` and `promtool test rules` validate the actual rule file and its logical-time fixtures. |
| Collector restart during backend outage | Tempo is stopped, known trace canaries are sent, the Collector is killed/restarted, Tempo is restored and the known IDs are queried. |
| Fluent Bit restart during backend outage | Loki is stopped, known logs are appended, Fluent Bit is killed/restarted, Loki is restored and delivery is queried. Duplicates are permitted. |
| Kafka consumer outage | Consumer is stopped while Fluent Bit sends known records; their event IDs are read from Kafka, then the consumer restarts, Loki delivery is checked and committed offsets must advance. |

The verification script saves its run timestamp and check outcomes to `artifacts/stack-verification.json`. Read the recorded results before saying these scenarios passed on a particular machine. Process/API success is not equivalent to a disk-loss test or a long-duration soak. SIGKILL tests preserve the same named volumes. SQLite, Loki and Kafka commits remain separate; a crash in their acknowledgement gap can duplicate records. Output limits and retention still bound how long replay is possible.

The authoritative assertions are the executable project/test/verifier code. A screenshot helps inspect the evidence, while raw reports, artifact contents and backend queries establish what was measured. A useful follow-up exercise is to change one assumption—outage duration, arrival rate, traffic sparsity, label vocabulary, evidence quality or worker cost—and explain which assertion and capacity decision must change with it.
