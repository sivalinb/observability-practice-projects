# Recorded verification

Verified on **18 October 2026** using Python 3.12, an Apple Silicon host and an isolated Linux/ARM64 Docker runtime. The core stack uses the pinned versions in the Dockerfiles. These are personal-scale experiments with synthetic workloads, not production benchmarks.

| Layer | Result | Evidence |
| --- | --- | --- |
| Python tests | **63 passed** | [Full output](evidence/unit-tests.txt) |
| Ten project executions | **76 scenario checks passed** | [Actual reports](evidence/project-reports.json) |
| Complete telemetry stack | **12 integration checks passed** | [Raw verification record](evidence/stack-verification.json) |
| Grafana data validation | Every project's numeric measurement equals its raw report; all latest-run scenarios have log evidence | [Actual data-source proxy responses](evidence/grafana-measurement-queries.json) |
| Prometheus rule evaluation | Six rules pass syntax and logical-window fixtures, including clock skew | Included in the stack record |
| Screenshots | One actual Grafana dashboard capture per project | [Capture manifest and hashes](evidence/screenshots.json) |
| Gradio | Actual recovery execution verified in the browser, 8/8 checks | [Screenshot](assets/gradio.jpg) |

## What the integration checks establish

1. All ten Python demos complete with passing scenario checks.
2. Prometheus scrapes the measured results, and their total matches the raw reports.
3. OpenTelemetry's independently exported check counter reaches Prometheus.
4. The wall-clock SLI source is scraped separately from logical-time alert fixtures.
5. Five unique canary event IDs reach Loki through Fluent Bit.
6. Known trace IDs sent through the Collector are retrievable in Tempo.
7. Grafana's own data-source proxies return every project's exact numeric values and latest-run scenario logs.
8. Three provisioned data sources work and all eleven dashboards exist. Tempo is checked through its backend proxy because this plugin version has no `/health` resource.
9. Promtool validates the real rules and evaluates the historical-window fixtures.
10. With Tempo stopped, five accepted traces survive a Collector **SIGKILL**, persistent queue reopening and backend restoration.
11. With Loki stopped, five unique canary logs survive a Fluent Bit **SIGKILL**, filesystem buffer reopening and backend restoration. Duplicate delivery remains permitted.
12. With the consumer stopped, five Fluent Bit records are observed in Kafka. After consumer restoration, all five unique IDs reach Loki and the committed offset advances to the broker's observed end offset.

The trace test uses synchronous canary exports and a Collector pipeline without the volatile batch processor. Ordinary Python SDK batch buffers remain volatile before export. The Fluent Bit test is a bounded local outage, not a power-loss or disk-loss test. Kafka has one broker and replication factor one. See [the durability boundaries](architecture.md) before expanding the design.

## Live model evaluation

Project 08's default deterministic evidence baseline passes its seven scenario checks. The optional local LLM adapter was actually exercised with Qwen 0.5B, 1.5B and 3B variants. **No successful LLM diagnosis is claimed:** each output failed the unchanged evidence validation policy at its evaluation point.

The final 3B response selected the expected dependency-failure hypothesis but omitted a supporting source required by this deliberately conservative policy. Other attempts produced inconsistent or unsupported explanations. The [sanitized model evaluation](evidence/model-evaluation.json) preserves the final response, rejection, model digest and evaluation limitations. Prompt/schema revisions mean these runs are not a controlled model-size benchmark.

An invalid model response fails the run and saves `model-rejection.json`; it is never converted to a passing diagnosis. This gives a concrete exercise in model evaluation, evidence sufficiency, over-conservative gates and the distinction between a plausible explanation and an accepted investigation result. A future policy change should be justified on held-out incident cases, not selected merely to make one response pass.

## Reproduce

```bash
python -m unittest discover -s tests -v
python run.py --all
python scripts/stack.py --replay up --build -d
python scripts/verify_stack.py --chaos --kafka
python scripts/record_evidence.py
```

The commands above regenerate measurements and evidence. The screenshot manifest refers to browser captures made during this verification; taking new screenshots is a separate browser action. Latency, throughput and instrumentation overhead vary between executions. Read each README's scenarios and limitations before interpreting a result.

GitHub Actions runs the Python and complete-stack checks on pushes and pull requests. The recorded local results above are distinct from a particular remote workflow's status.
