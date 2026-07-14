# Nordvik KYC Agent

Multi-agent KYC document processing pipeline built on **LangGraph**.
Takes a customer's KYC package (documents + declared data), classifies and
extracts fields, validates them against business rules and mock external
registries, scores risk and either decides automatically or escalates to a
compliance analyst (human-in-the-loop) — with a full audit trail for every
decision.

The full project specification (in Russian, the single source of truth) is
in [SPEC.md](SPEC.md).

## Quick start

```bash
docker compose up --build
```

That is all: the stack (FastAPI app + PostgreSQL) boots **without any API
keys** on a deterministic offline LLM provider. Real models are enabled by
configuration only — copy `.env.example` to `.env` and set `MODEL_*` +
provider API keys.

Try it:

```bash
# submit a clean case (auto-approve within seconds)
python3 - <<'EOF' > /tmp/package.json
import json
data = json.load(open("data/synthetic/golden_set.json"))
case = next(c for c in data["cases"] if c["case_id"] == "sanctions-hit")
print(json.dumps(case["package"]))
EOF
CASE_ID=$(curl -s -X POST localhost:8000/api/v1/cases \
  -H 'Content-Type: application/json' -d @/tmp/package.json | jq -r .case_id)

curl -s localhost:8000/api/v1/cases/$CASE_ID | jq          # awaiting_human_review
curl -s -X POST localhost:8000/api/v1/cases/$CASE_ID/review \
  -H 'Content-Type: application/json' \
  -d '{"outcome": "reject", "reviewer": "j.doe", "comment": "EU list match"}' | jq
curl -s localhost:8000/api/v1/cases/$CASE_ID/audit | jq    # full audit trail
```

The case survives an app restart between the escalation and the human
decision — state lives in the Postgres checkpointer, not in the process.

## Architecture

```
                        ┌──────────────────────────────────────────┐
                        │                 FastAPI                  │
                        │ POST /cases   GET /cases/{id}   /review  │
                        └───────────────┬──────────────────────────┘
                                        │ invoke / resume(Command)
                                        ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                          LangGraph StateGraph                           │
 │                                                                         │
 │ intake → router → orchestrator ══Send×N══► extract_document (workers)   │
 │                        │                          │  (fan-in)           │
 │                        │(incomplete)              ▼                     │
 │                        │                validator (agent-checks-agent)  │
 │                        │                          ▼                     │
 │                        │                risk_scorer ──► sanctions/PEP   │
 │                        ▼                          ▼        (mock tools) │
 │                  decision_gate ◄──────────────────┘                     │
 │                    │        └──► human_review (interrupt) ─► finalize   │
 │                    └──► auto_decision ─────────────────────► finalize   │
 │                                                                         │
 │  persistent step failure ──► handle_error ──► decision_gate (degraded)  │
 └───────────────────────────────────┬─────────────────────────────────────┘
                                     │ checkpoints + audit events
                                     ▼
                          PostgreSQL (16): checkpointer, audit_events
```

Implemented patterns (rationale in SPEC.md §4): routing,
orchestrator–workers (Send fan-out per document), structured output at
temperature 0, evaluator agent (extraction grounding check), human-in-the-loop
via `interrupt()`/`Command(resume)`, Postgres state persistence, bounded
execution with retry + model fallback + graceful degradation, trajectory
logging / audit trail, and per-step model right-sizing.

### Model right-sizing

Each step's model is an env-configured `provider:model` string resolved via
LangChain `init_chat_model` (no vendor hardcoded). Recommended production
tiering: a small model (`claude-haiku-4-5`) for document classification, a
stronger one (`claude-sonnet-5`) for extraction/validation/risk, and
`claude-opus-4-8` as the retry fallback. The special provider `fake:` is the
deterministic offline implementation used for tests, CI and this demo.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs it).

```bash
uv sync                                   # install deps
uv run pytest                             # 87 unit + integration tests
uv run ruff check src tests eval          # lint
uv run python eval/run_eval.py --quiet    # eval on the golden set
uv run uvicorn kyc_agent.api.main:app     # API against local Postgres
PERSISTENCE_BACKEND=memory uv run uvicorn kyc_agent.api.main:app  # no DB needed
```

### Project layout

```
src/kyc_agent/
├── api/            FastAPI app, case service (submit / status / review / SSE)
├── graph/          LangGraph state, context, nodes, assembly
├── llm/            step services, live + fake implementations, retry/fallback
├── rules/          deterministic business rules (pure, unit-tested)
├── tools/          mock sanctions / PEP registries
├── audit/          audit-trail sinks
├── persistence/    Postgres pool, checkpointer setup, audit storage
├── observability/  structlog config (JSON trajectory logs)
└── schemas/        Pydantic models (documents, case, decisions)
data/synthetic/     golden_set.json — 17 synthetic cases (no real PII)
eval/               golden-set generator, eval harness, results.json
tests/              unit + integration (graph flows, HITL, API)
```

## Evaluation

`uv run python eval/run_eval.py --quiet` replays the 17-case golden set
through the graph twice — full pipeline and a `--no-validator` ablation —
and writes [eval/results.json](eval/results.json). Current numbers
(deterministic fake provider; same command evaluates live models when
`MODEL_*` is configured):

| Metric | Full pipeline | No-validator ablation | Target |
|---|---|---|---|
| field_accuracy | **1.00** | 1.00 | ≥ 0.95 |
| auto_rate (typical subset) | **0.714** | 0.714 | 0.70–0.80 |
| escalation_recall | **1.00** | 0.857 | = 1.00 (hard invariant) |
| escalation_precision | **1.00** | 1.00 | ≥ 0.8 |
| decision_accuracy | **1.00** | 0.941 | ≥ 0.9 |
| false auto-approvals | **0** | 1 | 0 |

The ablation quantifies the evaluator ("agent checks agent") pattern: without
the grounding check, a hallucinated extraction that happens to match the
declared data sails through as a **false auto-approval** — exactly the
compliance failure mode the validator exists to prevent. The eval exits
non-zero if `escalation_recall` of the full pipeline is ever below 1.0.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/cases` | Submit a KYC package; `202` + `case_id`, runs in background |
| GET | `/api/v1/cases/{id}` | Status, decision, risk level, review payload if escalated |
| POST | `/api/v1/cases/{id}/review` | Human decision; resumes the interrupted graph |
| GET | `/api/v1/cases/{id}/audit` | Full audit trail |
| GET | `/api/v1/cases/{id}/events` | SSE stream of audit events |
| GET | `/health` | Liveness incl. DB probe |

Interactive docs: `http://localhost:8000/docs`.

## Observability

- **Audit trail (domain):** every node writes `audit_events` rows —
  `node_completed`, `rule_triggered`, `registry_checked`, `decision_made`,
  `human_decision`, `case_completed` — served via the API.
- **Trajectory logs (engineering):** structlog JSON on stdout.
- **LangSmith tracing:** set `LANGSMITH_TRACING=true` and
  `LANGSMITH_API_KEY` in `.env`; LangGraph reports every step natively.
  Traces appear under the `LANGSMITH_PROJECT` project name.

## Synthetic data

All cases in `data/synthetic/golden_set.json` are fully synthetic (no real
personal data) and regenerable via `uv run python eval/generate_golden_set.py`.
External sanctions/PEP registries are mocked with a realistic tool interface
(fuzzy search, latency-free, seeded failure injection); registry silence is
treated as a mandatory escalation, never as "clean".
