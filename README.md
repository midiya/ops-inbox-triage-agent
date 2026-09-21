# Ops Inbox Triage Agent

Turns a free-text internal support request into a correctly filed, correctly
prioritised Jira ticket — reliably, with a deliberate escape hatch for anything
it should not decide on its own.

Built as a study of what it takes to put an LLM in front of a real system that
writes real records: structured output, input validation, bounded retries, and a
dead-letter queue, rather than a prompt and a hope.

---

## The design principle

**The model does perception. Deterministic code makes the decisions.**

```
free text ──▶ CLASSIFY ──▶ ROUTE ──▶ FILE ──▶ Jira issue
               LLM +        pure      REST
               schema       Python
                  │           │         │
                  └─── retry · idempotency · dead-letter ───▶ dead_letter.jsonl
```

The LLM's only job is turning messy language into a typed object. Once that
object exists, routing it is an `if` statement — and an `if` statement is
testable, auditable, and cannot hallucinate. Handing the *decision* to the model
as well would buy nothing and cost every guarantee the system has.

The practical payoff: the entire routing policy is a pure function, so all of it
is unit-tested offline, in milliseconds, with no API key.

---

## What it actually does

**Structured output, not prompt-and-parse.** The Pydantic schema is sent as a
JSON Schema and generation is constrained to it, so malformed output is
prevented at the source instead of repaired afterwards. The result is validated
anyway — a schema constrains *shape*, not *truth*.

**Refuses rather than truncates.** Two size limits: over the soft limit it
classifies but forces human review; over the hard limit it refuses before
spending an API call. Silently classifying half a ticket produces a confident
wrong answer that nobody ever discovers.

**Treats ticket text as untrusted.** Input is wrapped in delimiters that are
stripped from the text first, so a message cannot close the tag early and escape
into the instruction context.

**Escalates instead of guessing.** Human review wins over everything, including
urgency. A critical ticket at 0.5 confidence is still escalated — the system must
not act hardest exactly where it is least certain.

**Bounded retries with jitter.** Three attempts, exponential backoff, full
jitter, and a `Retry-After` header always beats the computed delay. Only errors
that *can* succeed are retried; a 400 fails identically every time.

**A shared rate-limit gate.** A rate limit belongs to the account, not to one
call. When one ticket is throttled, every later ticket in the run waits for the
same window instead of rediscovering it and burning its own budget.

**Nothing is silently dropped.** Exhausted retries go to a dead-letter file with
the original text and the reason, so they can be replayed.

**Idempotent.** A ticket already filed is never filed twice, however many times
the batch is re-run.

**Extracts a stated estimate, and never invents one.** If the requester says
"about two days", that is extracted verbatim and converted to Jira's format
(`2d`) by a pure function, in English or Persian. If they state no duration, the
field stays empty. The model is explicitly told not to estimate the effort
itself: it does not know the team, the codebase, or who will do the work, and a
plausible number is worse than none because the next human to estimate anchors
on it.

---

## Measured, not assumed

| | |
|---|---|
| Offline tests, no API key needed | 110 (18 routing, 24 reliability, 17 access/spend guard, 51 estimate parsing) |
| Real output usage vs. configured ceiling | 55–83 tokens vs. 800 |
| Input tokens per call, of which the ticket is | ~1,045, of which ~10 |
| Persian vs. English token cost, same content | 3.05 vs. 4.85 chars/token (~1.6×) |
| Prompt-injection sample ticket | demands `finance_request`, `confidence 1.0`; gets `hardware_request`, `0.9` |

A fixed eval set (`samples/tickets.jsonl`, 15 tickets including several deliberately
nasty ones) means any prompt or schema change can be re-run and compared. Without
a fixed set, tuning a prompt is guessing.

Persian input classifies identically to English with no preprocessing, including
with heavy misspellings and speech-to-text-style text — only the model's own
confidence drops.

---

## Running it

```bash
uv venv
uv pip install -r requirements.txt
cp .env.example .env        # add your OPENAI_API_KEY
```

```bash
python -m triage.cli "my VPN stopped working and I cannot reach the admin panel"
python -m triage.cli --all           # the whole eval set, mock mode
python -m triage.cli --all -v        # with retry and escalation logging
python -m triage.cli --all --file    # actually create Jira issues
```

**Mock is the default; `--file` is opt-in.** A tool whose default action writes to
a real system is one you cannot safely re-run while debugging.

Jira is optional — leave those variables blank and classification and routing
still work.

```bash
python tests/test_router.py
python tests/test_reliability.py
python tests/test_guard.py
python tests/test_estimates.py
```

All four run offline with no key and no network.

---

## Known limitations

Stated deliberately, because knowing where the guarantees stop is the point.

- **The idempotency store is a local file.** Lose it and you lose the guard. It
  is also not safe with two workers running at once — that needs a database with
  a unique constraint on the ticket id, or a lookup against Jira's own labels.
- **The size guard overrides the model's `needs_human` flag inside the
  classifier.** Strictly, that is a routing decision leaking into the perception
  layer. The seam is deliberate, to keep one call path simple.
- **Persian dates are extracted but not resolved.** A Jalali date is captured
  verbatim, which is correct, but converting it to a real date is not implemented
  yet. That belongs in deterministic code, not in the model.
- **No labelled evaluation set.** Twelve fixed tickets and spot checks catch
  regressions; they do not support an accuracy claim.
- **One shared token for the HTTP service, and an in-memory request ceiling.**
  Adequate for a single service on a laptop. The limiter resets when the process
  restarts, and there are no per-caller credentials.
- **Self-reported confidence is not calibrated.** It is used as a cheap routing
  threshold and never quoted as a probability.
- **Estimate conversion assumes an 8-hour day and a 5-day week.** Those are
  Jira's defaults but a site can change them. Whole units are passed straight
  through (`"2 days"` becomes `"2d"`, never `"16h"`) so Jira applies its own
  definition; only a fraction such as `"half a day"` forces the assumption.
- **`timetracking` is off by default.** A stated estimate always goes into the
  description, and into Jira's Original estimate field only when
  `JIRA_SUPPORTS_TIMETRACKING=true`. Sending that field to a project without time
  tracking makes Jira reject the whole issue.

---

## Optional: Telegram bot setup

The core tool is a CLI and needs none of this. The optional demo adds a chat
front end — you message a Telegram bot, and it replies with the Jira key it
created. Telegram is the messenger; n8n carries the message to the service; all
the classification and decision logic stays in this repo.

```
you ──▶ Telegram bot ──▶ n8n ──▶ POST /triage (this service) ──▶ Jira
                                        │
                                        └──▶ reply with the issue key
```

### 1. Create your own bot

1. Message `@BotFather` on Telegram (https://t.me/BotFather).
2. Send `/newbot`, then choose a display name and a username ending in `bot`.
3. BotFather replies with a **token** that looks like `123456789:ABCdef...`.

**That token is a credential.** Anyone holding it controls the bot completely.
Never commit it, never paste it into a screenshot or an issue. If it leaks, send
`/revoke` to BotFather — the old token dies immediately and you get a new one.

Store it wherever n8n reads credentials from, not in this repo.

### 2. Expose the service

Telegram delivers updates by calling a public HTTPS URL, so `localhost` is not
reachable. For local development a quick tunnel is enough:

```bash
cloudflared tunnel --url http://localhost:5678
```

A quick tunnel gets a **new URL on every restart**, and n8n reads that URL once
at boot and registers it with Telegram. So the order is: tunnel first, then n8n.
Start them the other way round and messages are silently dropped — nothing
errors, they simply never arrive. This is the most likely thing to break the
demo.

### 3. Wire up n8n

Start n8n with the tunnel URL, add your bot token as a Telegram credential, and
build a three-node flow: **Telegram Trigger → HTTP Request → Telegram Send**.

The HTTP Request node posts to `http://localhost:8000/triage` with the message
text and a `source_id`, plus an `X-Triage-Token` header matching
`TRIAGE_SHARED_TOKEN`. Use `tg-<chat_id>-<message_id>` for the source id — it is
stable per message, which is what makes the idempotency guard work: the same
message re-delivered will not file a second ticket.

`demo.md` has the full startup sequence, verification commands, and the failure
modes worth knowing about.

### Protecting the endpoint

Be precise about what is exposed. The tunnel publishes **n8n** (port 5678), not
the triage service (port 8000). So the internet-facing surface is n8n's webhook;
`/triage` is only reachable from the machine itself.

Three layers guard the thing that costs money:

1. **`/triage` binds `127.0.0.1`.** n8n runs on the same machine, so nothing more
   is needed. With `0.0.0.0` anyone on the same wifi could call it directly.
2. **A shared token.** Every caller must send `X-Triage-Token` matching
   `TRIAGE_SHARED_TOKEN`. There is no default — unset means the endpoint refuses
   everything, because an endpoint that spends money must never be open by
   accident.
3. **A request ceiling** (`TRIAGE_MAX_PER_MINUTE`, `TRIAGE_MAX_PER_DAY`),
   enforced whoever is calling. This covers what a token cannot: the most likely
   way this ever runs up a bill is not an attacker but a stuck retry loop,
   arriving with perfectly valid credentials.

**None of that is the real guarantee.** Every one of those layers depends on this
code being correct. The control that does not is a **spending cap at the
provider**: give this app its own OpenAI project with its own key and a monthly
budget, and turn off auto-recharge. Then the worst case is requests start
failing, which is a bad afternoon rather than a bad invoice.

Operationally: take the tunnel down when you are done, and send `/deleteWebhook`
to stop Telegram delivering to a URL you no longer control.

---

## Layout

```
triage/
  schemas.py      the contract with the model — everything it may say
  classify.py     the one LLM call, plus input guards and error mapping
  router.py       pure decision policy (no I/O, fully unit-tested)
  jira_client.py  I/O shell — builds a payload and posts it, makes no decisions
  reliability.py  backoff, jitter, rate-limit gate, dead-letter queue
  estimates.py    converts a stated duration to Jira format (pure, EN + FA)
  guard.py        shared-token check and the spend ceiling for the HTTP service
  pipeline.py     classify → route → file, with retries around the I/O only
  exceptions.py   one error vocabulary; `retryable` is read by the retry layer
samples/          the fixed eval set
tests/            offline tests, no API key required
```

`SPEC.md` records the design decisions and why. `WORKFLOW.md` describes the
build method — small verified increments, and how to use an AI assistant without
losing ownership of the design.
