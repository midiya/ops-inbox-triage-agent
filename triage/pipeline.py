"""End-to-end: one ticket in, one filed issue out (or a dead-letter record).

    classify  ->  route  ->  file
    (LLM)        (pure)     (REST)

The two I/O steps are wrapped in `with_retry` **separately**, not as one unit.
That matters: retrying the whole ticket after a *Jira* failure would pay for a
second LLM call to fix an unrelated problem, and would re-roll a classification
that was already correct. Retry the call that failed, not the workflow.

Between routing and filing sits the idempotency guard, so a ticket that was
already filed is never filed twice - which is what makes the retries above safe
to have at all.
"""

import logging
from dataclasses import dataclass

from triage.classify import classify_ticket
from triage.exceptions import TriageError
from triage.jira_client import FiledIssue, browse_url, file_issue
from triage.reliability import (
    RateLimitGate,
    dead_letter,
    load_processed,
    mark_processed,
    with_retry,
)
from triage.router import Action, Decision, route
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    """What happened to one ticket. Every field is optional except the id and the
    status, because a failure that happened early has no classification to show.

    `status` is a plain string rather than an enum only because it is a reporting
    detail, not a decision anything branches on - the router's Decision enum is
    where the actual policy lives.
    """

    ticket_id: str
    status: str                       # filed | skipped | failed
    result: TriageResult | None = None
    action: Action | None = None
    issue: FiledIssue | None = None
    error: BaseException | None = None


def process_ticket(
    ticket_id: str,
    ticket_text: str,
    *,
    mock: bool = True,
    gate: RateLimitGate | None = None,
    processed: dict[str, str] | None = None,
) -> Outcome:
    """Run one ticket through the whole pipeline. Never raises.

    Returning an Outcome rather than raising is deliberate *here* and is not the
    contradiction of the earlier "raise, don't return None" argument that it looks
    like. The difference is what the caller can do about it: `classify_ticket`
    raises because its caller must handle the failure. This function's caller is a
    batch loop, where one ticket failing is an ordinary, expected event that must
    not stop the other eleven. Failure is normal here, so it is a return value.

    That is the same rule stated earlier: exceptions for the exceptional, return
    values for the routine.
    """
    if processed is None:
        processed = load_processed()

    # Idempotency, checked before any work: a ticket already filed is never filed
    # again, no matter how many times the batch is re-run or retried.
    if ticket_id in processed:
        prior_key = processed[ticket_id]
        logger.info("Ticket %s already filed as %s; skipping.", ticket_id, prior_key)
        # The prior issue is reported, not re-filed, so a duplicate submission
        # can be answered with the link instead of silence. `mocked=False` is a
        # fact rather than a guess: mark_processed is called only when
        # `not issue.mocked`, so a hit here was always a real issue.
        return Outcome(
            ticket_id=ticket_id,
            status="skipped",
            issue=FiledIssue(key=prior_key, url=browse_url(prior_key), mocked=False),
        )

    # --- 1. classify (retryable I/O) -------------------------------------
    try:
        result = with_retry(
            lambda: classify_ticket(ticket_text),
            description=f"classify {ticket_id}",
            gate=gate,
        )
    except TriageError as e:
        dead_letter(ticket_id, ticket_text, e)
        return Outcome(ticket_id=ticket_id, status="failed", error=e)

    # --- 2. route (pure, cannot fail) ------------------------------------
    action = route(result)

    # --- 3. file (retryable I/O) -----------------------------------------
    try:
        issue = with_retry(
            lambda: file_issue(action, ticket_id, ticket_text, mock=mock),
            description=f"file {ticket_id}",
            gate=gate,
        )
    except TriageError as e:
        # The classification succeeded and is worth keeping in the record, so
        # whoever replays this does not have to re-run the LLM to know what it
        # decided.
        dead_letter(ticket_id, ticket_text, e)
        return Outcome(
            ticket_id=ticket_id, status="failed", result=result, action=action, error=e
        )

    # Written only after a real success, and only for real issues: recording a
    # mock as processed would make the next real run skip the ticket forever.
    if not issue.mocked:
        mark_processed(ticket_id, issue.key)
        processed[ticket_id] = issue.key

    return Outcome(
        ticket_id=ticket_id,
        status="filed",
        result=result,
        action=action,
        issue=issue,
    )


def process_batch(
    tickets: list[dict], *, mock: bool = True
) -> list[Outcome]:
    """Run every ticket, sharing one rate-limit gate and one idempotency map.

    Sharing the gate is the whole point: a 429 discovered on ticket 3 pauses the
    run once, instead of each remaining ticket independently rediscovering it and
    burning its own retry budget to learn the same fact.
    """
    gate = RateLimitGate()
    processed = load_processed()

    return [
        process_ticket(
            t["id"], t["text"], mock=mock, gate=gate, processed=processed
        )
        for t in tickets
    ]
