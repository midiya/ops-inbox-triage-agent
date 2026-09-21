"""The contract between the LLM and the rest of the system.

Everything the model is allowed to say about an incoming ticket is declared
here. The API turns this into a JSON Schema and constrains generation to it, so
malformed output is prevented at the source rather than repaired afterwards.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    """The category an incoming internal request belongs to."""

    # Everything below this line is a note to us, not to the model. Pydantic
    # puts a class docstring into the JSON Schema as `description`, and that
    # description is sent to the model - so meta-commentary belongs in comments.
    #
    # Subclassing `str` makes the members serialise as their string values, so
    # `result.category == "bug_report"` works and JSON round-trips cleanly.
    #
    # This list is one view of an internal ops/IT inbox. It is the part most
    # worth changing for a different organisation: the categories decide what
    # the router can do, so they are the real domain model here.

    ACCESS_REQUEST = "access_request"                    # permissions, VPN, SSO, tool seats
    BUG_REPORT = "bug_report"                            # something internal is broken
    HARDWARE_REQUEST = "hardware_request"                # laptop, monitor, phone, repairs
    ONBOARDING_OFFBOARDING = "onboarding_offboarding"    # joiner/leaver logistics
    DATA_REQUEST = "data_request"                        # a report, export, or query
    HOW_TO_QUESTION = "how_to_question"                  # answerable from documentation
    FINANCE_REQUEST = "finance_request"                  # reimbursement, invoice, payment

    # The escape hatch. Without it, an off-topic ticket gets force-fitted into
    # the nearest category and nobody ever learns that it happened. The same idea
    # as an explicit out-of-domain class in a classifier: it lets the model
    # decline instead of guess.
    OTHER = "other"


class Urgency(str, Enum):
    """How urgent an incoming internal request is."""

    # The test for a good level set: you can write a one-line rule for telling
    # any two apart. If you cannot state the rule, the model cannot apply it,
    # and the labels you get back will be noise.

    CRITICAL = "critical"   # blocking many people now, or losing money/data
    HIGH = "high"           # blocking one person's core work now
    NORMAL = "normal"       # needed soon, a workaround exists
    LOW = "low"             # convenience, no deadline


class TriageResult(BaseModel):
    """The structured verdict on one incoming ticket."""

    # Strict structured outputs require additionalProperties: false, which
    # Pydantic emits only when extra fields are forbidden. Being explicit here
    # also means a model that invents a field fails loudly instead of silently.
    model_config = ConfigDict(extra="forbid")

    category: Category = Field(
        description=(
            "The single best-fitting category for this request. If the request "
            "does not clearly belong to any category, use 'other' rather than "
            "forcing the closest match."
        )
    )

    urgency: Urgency = Field(
        description=(
            "How urgent this is, judged only on evidence in the ticket text. "
            "Do not infer urgency from an emotional or insistent tone; a "
            "politely-worded outage is still critical, and an angry request for "
            "a second monitor is still low."
        )
    )

    summary: str = Field(
        description=(
            "One factual sentence, at most 25 words, stating what the requester "
            "needs. No greetings, no apologies, no restating the category. "
            "Written for an engineer skimming a queue of fifty of these."
        )
    )
    # Note this is `str`, not `str | None`. Every ticket has text, so a summary
    # is always producible; making it nullable hands the model a way to skip the
    # one field a human actually reads.

    # --- Extracted detail: named fields, never a free-form dict. -------------
    # `extracted_field: Dict[str, str]` would have reopened the hole this file
    # exists to close. A dict authorises any keys at all: call #1 returns
    # {"system": "VPN"}, call #2 returns {"affected_service": "vpn", "note":
    # "urgent!!"}, and no downstream code can rely on either. That is
    # unpredictable JSON wrapped in predictable JSON. Named fields let the router
    # read `result.affected_system` with no defensive lookups.
    #
    # All three are nullable on purpose: a ticket that mentions no deadline must
    # be able to say so. A required field with no honest answer is an invitation
    # to invent one.
    #
    # Note the shape `str | None` with no Python default. Strict structured
    # outputs require every property to appear in the schema's `required` list;
    # optionality is expressed as a union with null, not as a default value.

    affected_system: str | None = Field(
        description=(
            "The specific system, service, or tool named in the ticket (for "
            "example 'VPN', 'Jira', 'payroll portal'). Null if none is named. "
            "Do not guess a system from the category."
        )
    )

    affected_person: str | None = Field(
        description=(
            "The person the request is about, if it is someone other than the "
            "requester - for example a new joiner being onboarded. Null "
            "otherwise."
        )
    )

    deadline: str | None = Field(
        description=(
            "Any date or timeframe the requester states, copied verbatim from "
            "the ticket (for example 'by Sunday', 'before the 3rd'). Null if "
            "none is stated. Do not normalise it and do not invent one."
        )
    )
    # Deliberately a string, not a `date`. "next Friday" is not a date until you
    # know the sender's timezone and today's date - resolving it is deterministic
    # code's job, not the model's. Asking for a real date would make it guess,
    # and the guess would be schema-valid and wrong. Extract the raw span here;
    # parse it in Python where you can unit-test it.

    estimate: str | None = Field(
        description=(
            "How long the requester says the work will take, copied verbatim "
            "from the ticket (for example 'about two days', 'should take an "
            "hour'). Null unless the ticket actually states a duration. Do not "
            "estimate the effort yourself, and do not derive a duration from a "
            "deadline: 'needed by Sunday' is a deadline, not an estimate."
        )
    )
    # The instruction not to estimate is the whole point of this field, and it is
    # the opposite of what the name suggests, so it is worth being explicit.
    #
    # Extracting a stated duration is perception, which the model is good at.
    # Producing one is judgment it cannot have: it does not know the team, the
    # codebase, who will do the work, or what "done" means here. An estimate is a
    # team's shared agreement, and a plausible number from a model is worse than
    # no number, because whoever estimates next anchors on it without realising.
    #
    # Same shape as `deadline` for the same reason: raw text out of the model,
    # conversion in deterministic code (see triage/estimates.py) where "about two
    # days" becomes "2d" under unit test rather than under a language model.

    # --- Control signals ----------------------------------------------------

    confidence: float = Field(
        ge=0.0,
        le=1.0 ,
        description=(
            "How confident you are in the category and urgency above. Use a "
            "value below 0.7 when the ticket is ambiguous, mixes several "
            "unrelated requests, or lacks the detail needed to classify it."
        ),
    )
    # Read this before quoting the number to anyone: a self-reported confidence
    # is a token the model generated, not a calibrated probability, and it skews
    # overconfident. It is still useful as a cheap routing threshold - under 0.7,
    # send it to a human - because that catches a real share of errors for free.
    # It is not an accuracy figure and must never be reported as one.

    needs_human: bool = Field(
        description=(
            "True if this ticket should not be auto-filed: it is ambiguous, "
            "contains several unrelated requests, touches a sensitive matter "
            "(HR, payroll, personal data, a security incident), or asks for "
            "something irreversible."
        )
    )
    # Renamed from `human_escalation_flag`. The "_flag" suffix describes the
    # datatype, which `bool` already states; the name should describe the
    # decision being recorded.

    escalation_reason: str | None = Field(
        description=(
            "If needs_human is true, one short sentence saying why, addressed "
            "to the person who will pick it up. Null if needs_human is false."
        )
    )
    # A bare `True` is unactionable - whoever opens the queue needs to know what
    # to look at. This is a soft coupling, though: nothing stops the model
    # setting the flag and leaving the reason null. A Pydantic `model_validator`
    # could enforce the pair after parsing. Worth naming as a known gap even if
    # you leave it unimplemented; knowing where your guarantees stop is the
    # point.


# `summary` deliberately has no `max_length`. The 25-word limit lives in the
# description instead, because strict mode supports only a subset of JSON Schema
# keywords and I am not certain `maxLength` on strings is among them. An
# unsupported keyword is a 400 at request time, not a warning. We find out in
# step 2 - if it is supported, move the constraint into the field, where it is
# enforced rather than merely requested.
