# Demo runbook — Telegram bot to Jira ticket

Everything needed to bring the whole stack up from a cold machine, verify it, and
run the demo. Written to be followed literally, including the failure modes that
have actually bitten this setup rather than the ones that sound plausible.

---

## 0. The shape of it

```
Telegram  ──►  cloudflared  ──►  n8n        ──HTTP──►  FastAPI    ──►  OpenAI
(user)         (public URL)      (transport)           (decisions)     └─►  Jira
   ▲                                                        │
   └──────────────── reply: key + link ─────────────────────┘
```

In one sentence: **n8n owns transport, Python owns decisions.**
Routing rules have to be unit-testable and n8n IF-nodes are not, so the model
perceives, deterministic Python decides, and n8n only carries things.

---

## 1. One-time setup (already done — for a fresh machine)

| Thing | Command / where |
|---|---|
| Node 22+ | `winget install OpenJS.NodeJS.22` (n8n 2.x requires `>=22.22`) |
| cloudflared | `winget install Cloudflare.cloudflared` |
| Python deps | `pip install -r requirements.txt` in the venv |
| Secrets | `.env` with `OPENAI_API_KEY`, `JIRA_*` (see `.env.example`) |
| Telegram bot | `@BotFather` → `/newbot` (see README, *Telegram bot setup*) |

If `winget` is not recognised, it is installed but off PATH — call it as
`& "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"`, or add that folder to
your user PATH.

---

## 2. Startup sequence

Four terminals. **Order matters**: the tunnel URL must exist before n8n starts,
because n8n reads it once at boot and hands it to Telegram.

### Terminal 1 — the triage service

```powershell
cd path\to\ops_inbox_triage_agent
$env:TRIAGE_MOCK = "false"          # "true" = never touches Jira
python -m uvicorn service:app --host 127.0.0.1 --port 8000
```

* Run from the **project root** — `config.py` loads `.env` relative to the
  working directory and raises if the key is missing.
* `--host 127.0.0.1`, so the service is reachable only from this machine. n8n
  runs here too, so it needs nothing more. On shared wifi, `0.0.0.0` would let
  anyone on the network spend your OpenAI credit - use it only if n8n is ever
  containerised, and firewall the port if so.
* `/triage` requires an `X-Triage-Token` header matching `TRIAGE_SHARED_TOKEN`
  in `.env`, and refuses everything if that variable is unset.
* Check port 8000 is free first (`netstat -ano | findstr :8000`). A container
  publishing the same port will answer *instead* of this service, and both
  happen to expose `/health` — a confusing hour is available here for free.

Verify: `http://127.0.0.1:8000/health` → `{"status":"ok","mock":false}`

### Terminal 2 — the tunnel

```powershell
cloudflared tunnel --url http://localhost:5678
```

Copy the `https://<random-words>.trycloudflare.com` line it prints.

**A quick tunnel gets a new URL every restart.** That is the single most likely
thing to break this demo — see §5.

### Terminal 3 — n8n

```powershell
$env:N8N_WEBHOOK_URL = "https://<the-url-from-terminal-2>"
$env:N8N_DIAGNOSTICS_ENABLED = "false"     # silences noisy telemetry errors
npx n8n start
```

* `N8N_WEBHOOK_URL` is the current name; `WEBHOOK_URL` still works but warns.
* Without it, n8n registers `localhost` with Telegram and every message is
  silently dropped. Nothing errors. It just never arrives.
* `--tunnel` **no longer exists** in n8n 2.x. It is accepted and ignored, which
  is why cloudflared is doing this job.

Wait for `Editor is now accessible via:` followed by the tunnel URL — if it
prints `localhost` instead, the env var did not take.

### Terminal 4 — yours

For the CLI parts of the demo.

---

## 3. Verify before demoing

Run all four. Any failure here is far cheaper now than in front of an audience.

```powershell
# 1. service alive and in the mode you expect
curl.exe http://127.0.0.1:8000/health

# 2. tunnel reaches n8n (expect 200 and ~25 KB of editor HTML)
curl.exe -s -o NUL -w "%{http_code}" https://<tunnel>.trycloudflare.com/

# 3. Telegram has the CURRENT tunnel URL registered
curl.exe "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"

# 4. end to end: message the bot, expect a reply with a real SCRUM key
```

On check 3 look for three things: `url` matches today's tunnel,
`pending_update_count` is 0, and there is **no** `last_error_message`. A stale
`url` from a previous session is the classic silent failure.

Open the workflow from the n8n editor at `http://localhost:5678`. n8n 2.x uses
**publish**, not the old active/inactive toggle — a workflow that is not
published has no live trigger.

---

## 4. The demo

Four paths. The first is the headline; the rest are the reason to hire you.

### 4.1 Happy path — Telegram

Send to your bot:

> The VPN has been down since 9am and the whole Ops team cannot reach the
> internal wiki.

Reply comes back as key, category, urgency, summary, and a clickable Jira link.

> "Free text in, a correctly filed and prioritised Jira ticket out. The model
> only turned language into a typed object — a Pydantic model decided the
> priority and the project."

Open the link. A real issue, with the **original ticket text preserved verbatim**
in the description, not the model's paraphrase.

### 4.2 Human-in-the-loop — Telegram

Send a ticket of **4000–4096 characters** (paste a wall of text).

> "Over the soft limit, so it still gets classified, but deterministic code
> overrules the model and forces human review. We never truncate — silently
> classifying half a ticket produces a confident wrong answer and nobody ever
> finds out."

The reply carries `⚠️ Flagged for review`.

*Telegram caps messages at 4096 chars and the hard limit is 8000, so the
hard-refusal path is unreachable from the bot by construction. Demo it on the
CLI instead — and say so; knowing where a guard cannot fire is worth more than
pretending it can.*

### 4.3 Failure handling — CLI

```powershell
# hard size limit: refuses rather than guessing
python -m triage.cli (("word " * 2000))

# the full sample set, mock (nothing filed)
python -m triage.cli --all

# and for real
python -m triage.cli --all --file
```

> "Three things go wrong in production: the model returns something unusable,
> the API is down, or the input is not a real ticket. Each one is a different
> exception type, and the type decides whether it retries or dead-letters."

Show `dead_letter.jsonl`, and `triage/exceptions.py` — the transient/permanent
split, retryability read off the HTTP status rather than a hardcoded list of SDK
exception classes that would go stale every release.

### 4.4 Idempotency — CLI

```powershell
curl.exe -X POST http://127.0.0.1:8000/triage -H "Content-Type: application/json" `
  -H "X-Triage-Token: <the value from .env>" `
  -d "{\"text\":\"printer on floor 2 is jammed\",\"source_id\":\"demo-idem-1\"}"
# run it a second time
```

First call: `status: filed` with a new key. Second: `status: skipped`, the
**same** key and URL, and `classification: null` — the guard fires before the LLM
call, so the duplicate costs nothing and creates nothing.

> "Queue delivery is at-least-once, so the same ticket will arrive twice. This
> deduplicates by source id before any work happens."

**Be precise about the claim.** `source_id` is `tg-<chat>-<message_id>`, so this
protects against *Telegram re-delivering the same message* — which it does when a
webhook fails to return 200. It does **not** dedupe a user typing the same
complaint twice; that would be content-based dedup, a different decision, and
arguably the wrong one since two people reporting one outage may legitimately be
two tickets.

---

## 5. When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| Bot silent, no n8n execution | tunnel URL changed; Telegram holds the old one | restart n8n with the new `N8N_WEBHOOK_URL`, then unpublish/republish |
| `getWebhookInfo` shows old URL | same | as above |
| n8n executes, HTTP node `ECONNREFUSED` | service down or wrong port | check `/health` |
| Reply says `Could not triage` | look at `error.type` in the n8n execution | `APICallError` = provider; `ClassificationFailedError` = schema/prompt |
| `/health` answers but fields look wrong | **another process on port 8000** | `docker ps`, `netstat -ano | findstr :8000` |
| n8n starts but editor shows `localhost` | `N8N_WEBHOOK_URL` not set in *that* shell | env vars do not persist across terminals |

The tunnel-URL-changed case is the one to expect. Recovery is two minutes and
the checklist in §3 catches it before an audience does.

---

## 6. Teardown

Ctrl-C the three terminals. Then, if the bot should stop responding entirely:

```powershell
curl.exe "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

Delete the `SCRUM-*` issues the demo created. If the bot token was ever pasted
anywhere shared, `/revoke` in BotFather issues a new one and kills the old.

---

## 7. What is deliberately not here

Worth saying out loud — knowing where you stopped is a strength.

* **No queue broker or database.** A JSON-lines file plays both. The idempotency
  and dead-letter mechanics are real; the storage is not production-grade.
* **`/triage` is protected by a single shared token and a request ceiling.**
  Adequate for one service on one laptop. A real deployment wants per-caller
  credentials and a limiter that survives a restart. Fine on a laptop,
  not fine anywhere else — this is the first thing to add, and it is also the
  point at which an `api_exceptions.py` would finally earn its place.
* **A quick tunnel, not a named one.** Ephemeral by design.
* **n8n is not containerised.** Dev topology deliberately: fast iteration with
  `--reload` and a debugger. For deployment it would be one `docker-compose.yml`
  with both services on an internal network talking by service name, with no
  `host.docker.internal` anywhere — that is a Docker Desktop convenience which
  does not exist on plain Linux.
