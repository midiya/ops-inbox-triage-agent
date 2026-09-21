# Ops Inbox Triage Agent — Design Notes

Why the system is built the way it is. The README explains what it does; this
explains the decisions behind it, including the ones that were deliberately not
taken.

## 1. The problem

Internal teams (HR, Finance, IT, Ops) send free-text requests into a shared
inbox. A human reads each one, works out what it is, how urgent it is, and files
a ticket in the right place. That reading-and-filing step is repetitive, slow,
and inconsistent between people.

**This project automates that step: free text in, a correctly-filed,
correctly-prioritised Jira ticket out — reliably, without a human in the loop for
the routine cases, and with a deliberate escape hatch for the ones that need
one.**

## 2. The architecture, and the one principle behind it

```
free text ──▶ [1] PERCEIVE ──▶ [2] DECIDE ──▶ [3] ACT ──▶ Jira issue
                  LLM             plain          REST
              structured         Python           API
                output            rules
                    │                │              │
                    └────────── [4] RELIABILITY LAYER ─────────┐
                        validate · bounded retry · idempotency ·
                        dead-letter queue · structured logging  │
                                                                ▼
                                                        DLQ + human review
```

**The principle: the LLM does perception, deterministic code does decisions and
actions.**

The model's job is turning messy language into a typed object. Once that object
exists, routing it is an `if` statement — and an `if` statement is testable,
auditable, and cannot hallucinate. Handing the *decision* to the model as well
would buy nothing and cost every guarantee the system has.

The same rule decides smaller questions throughout. A stated deadline is
extracted as raw text and parsed in Python. A stated duration is extracted as
raw text and converted in Python. The model reads; it does not compute.

## 3. Scope

**In scope**

- One LLM call per ticket, structured output, Pydantic-validated.
- Deterministic routing rules from the validated object.
- One real external integration: create a Jira issue over REST.
- A reliability layer that survives: malformed model output, Jira being down,
  Jira returning 4xx, oversized input, duplicate submissions.
- A CLI: run one ticket, or a batch file of tickets.
- An HTTP endpoint with a shared token and a spend ceiling.
- Sample tickets, a README, and a runnable demo.

**Out of scope**, deliberately:

- No web UI, no queue broker, no database. A JSON-lines file plays the queue.
- No fine-tuning, no embeddings, no RAG.
- No multi-turn conversation with the requester.
- No per-caller credentials. One shared token, which is enough for one service
  on one machine and not enough for anything larger.

## 4. Definition of done

The system runs offline in mock mode and shows:

1. A messy ticket becoming a valid typed object.
2. That object becoming a real Jira issue you can open in a browser.
3. A ticket deliberately broken in several ways, and the system degrading
   gracefully each time instead of crashing.
4. A mock mode so the whole pipeline is demonstrable with no network and no
   credentials.

Point 3 is the one that matters. The happy path is the easy half.

## 5. How it was built

Each step was independently runnable before the next one started.

| # | Step | Definition of done |
|---|---|---|
| 1 | **Schema** — `triage/schemas.py` | A Pydantic `TriageResult` with categories, urgency, summary, extracted fields, and a human-escalation flag. No LLM yet. |
| 2 | **Classify** — `triage/classify.py` | Raw ticket text in, a validated `TriageResult` out, via structured output. Input guards and the full error taxonomy. |
| 3 | **Samples + CLI** — `samples/tickets.jsonl`, `triage/cli.py` | A fixed eval set including deliberately nasty tickets. CLI runs one ticket or the whole file. |
| 4 | **Route** — `triage/router.py` | Pure function: `TriageResult` → an `Action`. No I/O, fully unit-testable. |
| 5 | **Act** — `triage/jira_client.py` | Creates a real Jira issue over REST. Mock mode returns a fake key without network. |
| 6 | **Reliability** — `triage/reliability.py` | Bounded retry with backoff and jitter; retryable vs terminal failures; idempotency; a dead-letter file. |
| 7 | **Protect** — `triage/guard.py` | Shared-token auth and a rolling spend ceiling on the HTTP endpoint. |

## 6. Stack

- Python 3.14, environment managed with `uv`.
- `openai` — `client.responses.parse(..., text_format=TriageResult)`.
- `pydantic`, `python-dotenv`, `httpx`, `fastapi` for the HTTP service.
- Model: `gpt-4o-mini`.
- Secrets: `.env`, gitignored, loaded via dotenv. `.env.example` committed.

The structured-output and reliability patterns here are provider-agnostic.
OpenAI was used because a key was available; the same design maps onto any
vendor that supports schema-constrained output and returns HTTP status codes.

## 7. Decisions worth knowing about

**Refuse rather than truncate.** An oversized ticket is rejected before the API
call. Silently classifying half a ticket produces a confident wrong answer that
nobody ever discovers, which is worse than an error.

**Human review wins over urgency.** A critical ticket at low confidence is still
escalated. The system must not act hardest exactly where it is least certain.

**Retry only what can succeed.** A 400 fails identically every time, so retrying
it spends the budget to be told no again. The error type carries `retryable`;
the retry layer only reads it.

**The rate-limit gate is shared across a run.** A retry budget belongs to one
call, but a rate limit belongs to the whole account. Without a shared gate, every
ticket rediscovers the same limit and burns its own attempts learning it.

**The HTTP endpoint fails closed.** No token configured means no caller is
allowed, rather than every caller being allowed.
