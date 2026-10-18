# Illustrated architecture stories

These ten images were generated with the built-in image-generation tool, then optimized to WebP for repository delivery. Full-resolution originals were retained separately during creation. They are conceptual memory aids; the source configurations and READMEs establish actual component placement and delivery guarantees. Grafana and Gradio screenshots are captured from running software and are separate assets.

Each project stores its generated picture at `assets/architecture.webp`. The common visual direction is an isometric, cinematic learning scene with a navy backdrop, amber risk and teal recovery. Physical metaphors make the sequence memorable without suggesting that an illustration proves correctness.

## Generation prompt set

### 01 · Telemetry Recovery

Use case: scientific-educational. Create a wide 1536x1024 richly illustrated architecture picture for a Python learning project titled 'Telemetry Recovery'. A cinematic cutaway story in three continuous scenes: left small Python services emitting glowing envelopes to an OpenTelemetry gateway with a visible disk journal whose pages are numbered; center a storm snaps a network bridge while envelopes safely accumulate in the journal and a Fluent Bit courier keeps structured log crates in a filesystem warehouse; right after the storm the repaired bridge carries controlled replay, a Kafka railway holds durable numbered cars, and Grafana windows light up with recovered signals. Use short exact labels: 'Python', 'OpenTelemetry', 'WAL', 'Fluent Bit', 'Kafka', 'Grafana'. Include tiny visual acknowledgment stamps and duplicate envelopes being merged at destination. Editorial isometric illustration, navy background, amber danger, teal recovery, warm lighting, sophisticated educational poster, readable at README width. Show finite storage by an actual capacity gauge. It should feel like a memorable mind movie of a failure and recovery, not rectangles and arrows or a generic block diagram. No claims of zero loss or exactly once. No other prose.

### Shared wrapper for 02–10

The following wrapper was used with each project's title and scene below:

> Use case: scientific-educational. Create a wide 1536x1024 richly illustrated architecture picture for a Python personal learning project titled '[title]'. Scene: [scene] Style: sophisticated cinematic isometric educational illustration, navy backdrop, amber for risk, teal for successful operation, warm detailed lighting. Make the architecture a memorable mind movie, a continuous narrative scene with actual objects and physical metaphors, not rectangles and arrows, not a flowchart or block diagram. Accurate failure boundaries, no magical promises. Restrict text to the short labels requested, large and readable. No additional prose.

### 02 · Metric Quality Checker

A developer forges glowing metric instruments at a Python workbench. A magnifying-glass customs officer inspects name, unit and owner stamps against an open contract book. One crate is a branching vine of user IDs overflowing a series capacity gauge; it is stopped at an amber gate, while bounded route and status tags pass to OpenTelemetry and a Grafana window. Show visible before and after with pruning of unbounded labels. Exact short labels: Python, Contract, Cardinality, OpenTelemetry, Grafana.

### 03 · Telemetry Accuracy Checker

A lighthouse sends numbered teal pulse envelopes with known values through a collection canal. A conveyor drops one, another arrives stale like an old clock, another has a wrong label. At a Grafana viewing window an inspector compares actual received pulses against the original ledger; servers are green but the missing pulses trigger an independent red beacon on a separate island. Exact short labels: Canary, OpenTelemetry, Query, Grafana, Independent Check.

### 04 · Service Health Alerts

A miniature city of users crosses a service bridge. Tiny scattered red failures consume a few grains from an error-budget hourglass, then a sustained red wave drains it fast. Two clocks, a long window and a short window, jointly release a single useful bell to an engineer reading Grafana. A brief harmless storm passes without ringing. Exact short labels: Python, SLO, Error Budget, Burn Rate, Grafana.

### 05 · Shared Platform Protection

Three colored tenants enter a shared telemetry railway station. One tenant sends a huge flood of crates, contained behind a quota turnstile; the other two retain clear tracks and balanced round-robin dispatch. A guard checks identity keys rather than the labels painted on boxes. Query workers have visibly limited seats and a costly oversized query is refused. Exact short labels: Identity, Quotas, Fair Queue, OpenTelemetry, Grafana.

### 06 · Performance Troubleshooter

A detective follows one glowing trace ribbon through a Python request entering a shop, a visibly slow external dependency with an hourglass and a hot CPU engine with a flame. Above, a trace lens shows parent and child nested ribbons; below, a profiler lens shows a flame graph. Sampling sieves retain rare red error traces and slow amber traces. Fluent Bit carries logs to a Grafana evidence desk. Exact short labels: Python, Traces, Profiles, Fluent Bit, Grafana.

### 07 · Telemetry Cost Optimizer

A river of telemetry crates passes three physical storage terraces: hot precise recent data, warm compressed organized archives, cold broad historical layers. An engineer weighs storage cost coins against incident clues under a magnifier. A careless averaging mill destroys a rare latency spike; an alternative histogram jar keeps the clue. Kafka railway permits replay of original evidence. Exact short labels: Raw, Compression, Retention, Histograms, Kafka, Grafana.

### 08 · AI Incident Investigator

An engineer and a small AI assistant stand at an investigation table with separate metrics, logs, traces and runbook evidence cards. Every card has an evidence ID stamp. Two competing possible-cause paths are built with yarn, one refuted by an opposing evidence card; an unknown case stays behind a question mark sign. A locked control panel remains beyond the read-only evidence desk. OpenTelemetry and Fluent Bit couriers bring clues, Grafana displays them. Exact short labels: Evidence, Hypotheses, Verify, Read Only, Grafana.

### 09 · AI Performance Tracker

A tiny language-model workshop shows prompts waiting in a finite queue, then entering a prefill preparation oven and exiting a decode conveyor one token at a time. Separate stopwatch lenses measure waiting, first token and gaps between tokens. A Python CPU engine drives this small scene; far away a translucent optional GPU engine is labeled Optional GPU. Gauges and Grafana show throughput and latency under growing concurrency. Exact short labels: Queue, Prefill, Decode, First Token, Python, Grafana, Optional GPU.

### 10 · Service Monitoring Starter Kit

A developer plants a small service contract seed into a Python template printing press. The press unfolds a complete miniature service house with integrated OpenTelemetry wiring, a Fluent Bit courier, Grafana windows, SLO bell and ownership plaque. A checklist gate validates the resulting service before its first request lights up an end-to-end trace. Clearly a reusable toolkit with measured onboarding stopwatch. Exact short labels: Contract, Python, OpenTelemetry, Fluent Bit, Grafana, SLO.
