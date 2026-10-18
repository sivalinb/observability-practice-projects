# AI Incident Investigator

Investigate an incident by retrieving tenant-scoped evidence, comparing competing explanations and abstaining when support is missing or contradictory. The default execution is a **deterministic evidence baseline**. A real Ollama or OpenAI-compatible language model can be enabled explicitly; its answer must pass the same provenance and sufficiency gate.

![Illustrated architecture: an investigator walks through a room of metrics, trace clues, log fragments and competing explanations](assets/architecture.webp)

![Demo screenshot from the executed investigation and adversarial checks](assets/demo.jpg)

## Run the complete baseline

```sh
python run.py --project 08
python -m unittest discover -s tests -p 'test_08*.py' -v
```

Follow `artifacts/08/latest.json` to the latest run directory. Inspect `evidence.json`, `investigations.json` and the human-readable `investigation.md`. The baseline is functional without a model download or API credentials. Its score is a transparent rule weight, not model confidence. It is deliberately labeled as a baseline in the report and UI.

For a running Ollama endpoint with a model already installed:

```sh
INVESTIGATOR_PROVIDER=ollama \
INVESTIGATOR_ENDPOINT=http://127.0.0.1:21434 \
INVESTIGATOR_MODEL=qwen2.5:0.5b \
python run.py --project 08
```

For an OpenAI-compatible chat endpoint, set `INVESTIGATOR_PROVIDER=openai-compatible`, `INVESTIGATOR_ENDPOINT` to its API base including `/v1` if required, and `INVESTIGATOR_MODEL` to an installed model. A bearer token is read from `INVESTIGATOR_API_KEY` when needed. Credentials are never written into artifacts. Both adapters make real HTTP requests. `model-investigation.json` exists only after a successful, validated live model invocation; connection errors and validation failures fail the run. Nothing silently substitutes a fabricated model answer.

## The principal engineer thought process

Begin with the cost of being wrong. An investigator that sounds decisive but fabricates a cause creates more work during an incident. The useful first product is read-only, can cite every observation, keeps alternatives visible, scopes data to the right tenant and time, and says “insufficient evidence.” Automated remediation is outside this project.

Separate four concerns: retrieval, the observations that support or contradict each hypothesis, natural-language synthesis, and output validation. The baseline makes each boundary inspectable. A model can improve synthesis and interpretation, but it does not get authority to invent provenance or issue operational actions.

The synthetic evidence includes Prometheus-style metrics, Tempo-style spans, Fluent Bit log records and deployment history. Tenant `south` has a healthy payment observation; the incident is for `north`, so that record is excluded before scoring. A malicious instruction embedded in a request log is treated as a log string. No tool executes it.

Retrieval uses token overlap weighted by inverse document frequency and stable tie ordering. It is intentionally small and inspectable. The baseline then uses structured observation weights attached to the synthetic fixture. Those weights are **ground-truth annotations for this teaching dataset**, not a general parser that can diagnose arbitrary production logs. A real deployment needs typed extraction, event normalization, freshness checks and evaluation on incidents it has never seen.

## Decision and validation rules

A supported conclusion requires at least two supporting evidence IDs, score of at least four, a three-point margin over the next hypothesis, and no direct contradicting observation. The output retains dependency failure, service saturation and bad deployment as alternatives with explicit supporting and contradicting IDs. These thresholds are an illustrative policy, not calibrated statistical confidence.

The model output must contain a known conclusion or null, at least two distinct alternatives, valid retrieved evidence IDs, and an allowed read-only next step. The Ollama adapter additionally supplies a JSON schema with exact status/action enums, three alternative slots and hypothesis-specific supporting/contradicting citation sets. The independent validator rejects duplicate alternatives, invented IDs, citations with the wrong observation/sign, unsupported causes and actions such as deleting a database. A proposed conclusion must also pass the evidence baseline's sufficiency gate. This intentionally constrains model autonomy: the model cannot override known contradictory evidence just by writing a persuasive explanation.

The model interprets a labeled synthetic evidence packet; deterministic rules decide which conclusions have sufficient support. The schema permits every hypothesis and abstention, so it does not force the expected cause. A valid model response demonstrates constrained interpretation and provenance, not independent proof of a production root cause. A malformed or unsupported response fails the actual run; no answer is rewritten to make the model pass.

Alternatives considered include a free-form chat answer, a model that selects remediation tools, and a deterministic rules engine alone. Free-form answers are hard to evaluate; remediation adds a separate authorization and correctness problem; rules alone limit interpretation of new evidence. The chosen design preserves a reproducible baseline and permits measured model comparisons behind a shared validation contract.

## Scenarios covered and tested

| Scenario | Expected behavior |
|---|---|
| Known dependency incident | Multiple independent evidence kinds support dependency failure |
| Missing metrics/traces | Abstain and request more evidence |
| Direct contradictory probe | Abstain instead of hiding the contradiction |
| Wrong-tenant evidence | Excluded before ranking and scoring |
| Prompt injection inside logs | Preserved as data; cannot change the baseline conclusion |
| Invented evidence citation | Reject the answer |
| Unsafe recommendation | Reject the answer |
| Unsupported model cause | Reject even if the output is valid JSON |
| Duplicate evidence IDs | Reject rather than count copies as independent support |
| Repeatable model-shaped answer | Validates the schema/provenance path without pretending a mock is an LLM |
| Optional live model | Real endpoint answer must pass all gates before persistence |

The default report executes seven scenario checks. Unit tests cover the output validator and duplicate-evidence case in addition to running the complete baseline. A schema-valid answer is still not proof of factual correctness; the sufficiency gate is only as good as its observations.

## Stack and durability decision

Python owns retrieval, evaluation and the HTTP model adapters. The repository's OpenTelemetry pipeline records project and check spans, Fluent Bit carries structured outcome logs, and Grafana exposes evaluation status. These fixtures are synthetic and intentionally checked in so that every result is reproducible.

Do not add a WAL to an in-memory hypothesis list: it can be regenerated from the evidence snapshot. Preserve the evidence before investigation begins. The shared pipeline demonstrates filesystem queues and WAL at that boundary. Kafka becomes useful if evidence snapshots arrive continuously and several investigator/evaluator consumers require independent replay. It is not required to search seven local observations.

## Rollout, evaluation and interview narrative

Run the baseline against incidents with known outcomes before adding a model. Measure unsupported-conclusion rate, useful abstention, citation validity, tenant leakage, retrieved-source coverage and investigation time. Compare the model to the baseline on held-out cases. A more fluent report without better diagnostic accuracy is not an improvement. Start read-only, add operator feedback and preserve the full evidence snapshot and model version for reproducibility.

In an interview, walk through the contradictory-probe case first: there is enough tempting evidence to make a confident claim, but direct disagreement blocks it. Explain how untrusted log content differs from trusted policy, why provenance checks alone do not establish truth, and how a deterministic baseline helps quantify whether an LLM adds value.

Limitations: local lexical retrieval lacks semantic understanding; structured signal annotations are synthetic ground truth; the baseline is not a general incident classifier; live model quality depends on the selected endpoint. No live-model result is claimed unless an actual invocation artifact is present. This system makes no production change and sends no incident messages.
