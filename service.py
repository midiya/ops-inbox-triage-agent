"""HTTP front door for the triage pipeline.

n8n owns transport - Telegram in, Telegram out. This owns nothing but the
mapping between them. That is the SPEC.md principle one layer further out: the
LLM perceives, deterministic Python decides, and n8n only carries things.
Routing rules must be unit-testable, and n8n IF-nodes are not.

If logic ever starts accumulating in this file, it belongs in the pipeline.

Status codes carry transport meaning only:

  * 200 - the pipeline ran and reached a verdict, *including* `failed`
  * 422 - the request body was malformed (FastAPI raises this itself)
  * 500 - a bug; `process_ticket` never raises, so anything escaping it is one

A failed triage is deliberately not a 5xx. `with_retry` has already spent its
budget inside the pipeline, so answering 503 would invite n8n to retry work that
is definitively finished - rebuilding the double-retry problem we removed from
the OpenAI client. Retry policy stays in one place, in Python, where it is
tested. n8n branches on `status` in the body, not on the HTTP code.

Bind to loopback. n8n runs on the same machine, so there is no reason for this
to be reachable from the local network - and on shared wifi, `0.0.0.0` means
anyone on the network can spend your OpenAI credit. Use `0.0.0.0` only if n8n is
ever containerised, and put a firewall rule in front of it if so.

Run from the project root so dotenv finds .env:

    uvicorn service:app --host 127.0.0.1 --port 8000 --reload
"""

import logging
import os

from fastapi import Depends, FastAPI, Header, HTTPException

from api_schemas import TriageRequest, TriageResponse
from triage.config import (
    TRIAGE_MAX_PER_DAY,
    TRIAGE_MAX_PER_MINUTE,
    TRIAGE_SHARED_TOKEN,
)
from triage.guard import CostGuard, token_matches
from triage.pipeline import process_ticket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("triage.service")

# Operational mode belongs to the operator, not the caller. Were this settable
# from the request body, a stray Telegram message could file a real Jira issue.
# It defaults to mock, so the irreversible direction takes a deliberate act.
MOCK = os.getenv("TRIAGE_MOCK", "true").strip().lower() not in ("0", "false", "no")

app = FastAPI(title="Ops Inbox Triage", version="1.0.0")

cost_guard = CostGuard(
    per_minute=TRIAGE_MAX_PER_MINUTE, per_day=TRIAGE_MAX_PER_DAY
)

if not TRIAGE_SHARED_TOKEN:
    logger.error(
        "TRIAGE_SHARED_TOKEN is not set. /triage will refuse every request. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )


def require_token(x_triage_token: str | None = Header(default=None)) -> None:
    """Reject any caller that cannot present the shared secret.

    Fails closed when unconfigured: no token in the environment means no caller
    is authorised, rather than every caller being authorised. The 503 says the
    server is misconfigured, which is true and is a different problem from the
    401 a wrong token gets - and telling them apart matters at 2am.

    The same generic message goes to every rejected caller. Distinguishing
    "no header" from "wrong value" would confirm to a prober that the header
    name is right.
    """
    if not TRIAGE_SHARED_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Service is not configured for authenticated access.",
        )

    if not token_matches(TRIAGE_SHARED_TOKEN, x_triage_token):
        raise HTTPException(status_code=401, detail="Invalid or missing token.")


@app.get("/health")
def health() -> dict:
    """Liveness only.

    Deliberately does not call OpenAI or Jira. A health check that costs money
    and can be rate-limited is a liability; this exists to answer "is the
    service up" before you start blaming n8n.
    """
    return {
        "status": "ok",
        "mock": MOCK,
        "authenticated": bool(TRIAGE_SHARED_TOKEN),
        "usage": cost_guard.snapshot(),
    }


@app.post(
    "/triage",
    response_model=TriageResponse,
    dependencies=[Depends(require_token)],
)
def triage(req: TriageRequest) -> TriageResponse:
    # Checked after the token, so an unauthorised caller cannot consume the
    # budget of a legitimate one simply by flooding the endpoint.
    refusal = cost_guard.check()
    if refusal is not None:
        logger.warning("triage_refused id=%s reason=%s", req.source_id, refusal)
        raise HTTPException(status_code=429, detail=refusal)

    outcome = process_ticket(req.source_id, req.text, mock=MOCK)

    logger.info(
        "triage_completed id=%s status=%s issue=%s",
        outcome.ticket_id,
        outcome.status,
        outcome.issue.key if outcome.issue else None,
    )
    return TriageResponse.from_outcome(outcome)
