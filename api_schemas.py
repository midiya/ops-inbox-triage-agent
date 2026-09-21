"""The contract between this service and its HTTP callers.

Deliberately separate from triage/schemas.py, which is the contract with the
*model*. The two look alike and are not: schemas.py is serialised into the JSON
Schema sent to OpenAI and changes when the categories change; this file changes
when the API changes. Merging them would put HTTP-shaped fields into the model's
prompt, and would make schemas.py's own docstring untrue.

It lives at the project root rather than inside `triage/` for the same reason
exceptions.py imports no vendor SDK: the package must not know who is calling
it. Dependencies here run one way only - api_schemas -> triage - and never back.

There is no matching `api_exceptions.py` on purpose. This layer has no failure
modes of its own: a pipeline failure is a *result* (status="failed", HTTP 200),
a malformed body is FastAPI's RequestValidationError, and anything else is a
bug that should surface with its real type. `ErrorOut` below is the
serialisation of an exception, not an exception - JSON has no such concept.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Import only for type checking so this module stays importable without a
    # configured .env - `from_outcome` needs the object's fields at runtime, not
    # its class. That keeps the wire contract testable on its own.
    from triage.pipeline import Outcome


class TriageRequest(BaseModel):
    text: str = Field(min_length=1, description="Raw ticket text. Untrusted.")
    source_id: str = Field(
        min_length=1,
        description=(
            "The caller's unique id for this message - for Telegram, chat id "
            "plus message id. This is the idempotency key: the same value twice "
            "is answered with the original issue and never filed again."
        ),
    )


class IssueOut(BaseModel):
    key: str
    url: str
    mocked: bool
    # `mocked` crosses the wire for the same reason FiledIssue carries it: a
    # demo must never be able to pass a fake issue off as a real one.


class ClassificationOut(BaseModel):
    """A flattened view of TriageResult - deliberately not TriageResult itself.

    Re-exporting the model's schema here would weld the API to the prompt: every
    new field or renamed category would silently become a breaking API change.
    This is a projection, and the mapping below is where that decoupling is paid
    for.
    """

    category: str
    urgency: str
    summary: str
    needs_human: bool
    confidence: float
    escalation_reason: str | None = None


class ErrorOut(BaseModel):
    type: str
    message: str
    retryable: bool


class TriageResponse(BaseModel):
    status: str  # filed | skipped | failed
    ticket_id: str
    issue: IssueOut | None = None
    classification: ClassificationOut | None = None
    error: ErrorOut | None = None

    @classmethod
    def from_outcome(cls, outcome: "Outcome") -> "TriageResponse":
        """Domain object to wire shape. Pure: no server, no network, no I/O, so
        every branch of it is testable directly."""
        issue = (
            IssueOut(
                key=outcome.issue.key,
                url=outcome.issue.url,
                mocked=outcome.issue.mocked,
            )
            if outcome.issue
            else None
        )

        classification = None
        if outcome.result is not None:
            r = outcome.result
            classification = ClassificationOut(
                # The enums subclass str, so `.value` is redundant at runtime -
                # it is here to state which representation crosses the wire
                # rather than leaving it to an inheritance detail.
                category=r.category.value,
                urgency=r.urgency.value,
                summary=r.summary,
                needs_human=r.needs_human,
                confidence=r.confidence,
                escalation_reason=r.escalation_reason,
            )

        error = None
        if outcome.error is not None:
            e = outcome.error
            error = ErrorOut(
                type=type(e).__name__,
                message=str(e),
                # Reported for the operator, not as an instruction to the
                # caller: the retries this refers to are already spent. It tells
                # whoever reads the dead-letter file whether a replay is worth
                # attempting.
                retryable=bool(getattr(e, "retryable", False)),
            )

        return cls(
            status=outcome.status,
            ticket_id=outcome.ticket_id,
            issue=issue,
            classification=classification,
            error=error,
        )
