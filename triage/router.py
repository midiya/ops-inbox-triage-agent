"""Turns a classification into a decision.

Everything here is a pure function: no network, no files, no clock, no
randomness. That is deliberate and it is the point of the module. The LLM does
perception - unstructured text to a typed object - and this layer makes the
decisions, because a decision made by an `if` statement is testable, auditable,
and cannot hallucinate.

It also means the whole routing policy is testable in milliseconds with no API
key: build a TriageResult by hand, call `route`, assert on the Action.
"""

from dataclasses import dataclass
from enum import Enum

from triage.estimates import normalise_estimate
from triage.schemas import Category, TriageResult, Urgency

# The confidence below which we do not act automatically. Not a calibrated
# probability - it is a self-reported number from the model and it skews
# overconfident - so it is used as a cheap routing threshold and never quoted as
# an accuracy figure.
CONFIDENCE_FLOOR = 0.7


class Decision(str, Enum):
    """What the system has decided to do with a ticket."""

    AUTO_FILE = "auto_file"   # file it and let it flow to the owning team
    ESCALATE = "escalate"     # file it, but a human must look before anyone acts


@dataclass(frozen=True)
class Action:
    """A complete instruction for the acting layer.

    Frozen because an action is a decision that has already been made: if a later
    stage could edit it, the audit trail would be a lie.

    Note there is no project key. There is one Jira project and step 5 already
    knows it from config - this layer decides *what kind of thing this is*, the
    client decides *where it goes*. Keeping the destination out of the decision
    means the routing policy is testable without knowing anything about Jira.
    """

    decision: Decision
    issue_type: str
    priority: str
    labels: tuple[str, ...]
    summary: str
    reason: str | None = None   # why it was escalated; None when auto-filed
    estimate: str | None = None  # Jira duration, e.g. "2d"; None if not stated


# The issue type strings must match what the Jira project actually offers.
# Verified against GET /rest/api/3/issue/createmeta rather than assumed: this
# team-managed project has Epic, Subtask, Task, Story, Feature, Request and Bug -
# there is no "Service Request", which is what a reasonable guess would have
# produced and what would have failed at runtime.
#
# Policy as data rather than a chain of if/elif: adding a category is a one-line
# change, and someone who does not read Python can still check the table. Same
# idea as a config-driven rules engine: keep the policy declarative, keep the
# engine generic. Moving this table into a JSON file that a non-engineer edits
# is a config change from here, not a rewrite.
CATEGORY_ROUTING: dict[Category, tuple[str, tuple[str, ...]]] = {
    #                                  issue type          extra labels
    Category.ACCESS_REQUEST:         ("Request",("team-it",)),
    Category.BUG_REPORT:             ("Bug",             ("team-it",)),
    Category.HARDWARE_REQUEST:       ("Request",("team-it", "hardware")),
    Category.ONBOARDING_OFFBOARDING: ("Task",            ("team-hr",)),
    Category.DATA_REQUEST:           ("Task",            ("team-data",)),
    Category.HOW_TO_QUESTION:        ("Task",            ("team-it", "question")),
    Category.FINANCE_REQUEST:        ("Task",            ("team-finance",)),
    Category.OTHER:                  ("Task",            ("unrouted",)),
}

URGENCY_TO_PRIORITY: dict[Urgency, str] = {
    Urgency.CRITICAL: "Highest",
    Urgency.HIGH:     "High",
    Urgency.NORMAL:   "Medium",
    Urgency.LOW:      "Low",
}


def needs_review(result: TriageResult) -> str | None:
    """Return why a human must see this first, or None if it can be automated.

    Split out from `route` so the policy can be read and tested on its own, and
    so the reason comes from the same code that makes the decision - a flag with
    no reason is unactionable for whoever picks the ticket up.

    Order affects only the message: the first matching condition is the one
    reported, so the most specific reason is checked first.
    """
    if result.needs_human:
        # The model's own judgment, possibly overridden upward by the size guard
        # in classify_ticket. Either way, something asked for a human.
        return result.escalation_reason or "Flagged for human review."

    if result.confidence < CONFIDENCE_FLOOR:
        return (
            f"Low classification confidence "
            f"({result.confidence:.2f} < {CONFIDENCE_FLOOR})."
        )

    if result.category is Category.OTHER:
        # By definition this matched none of the known categories, so there is no
        # team to route it to. Auto-filing would just move the problem into a
        # queue nobody owns.
        return "Ticket did not match any known category."

    return None


def route(result: TriageResult) -> Action:
    """Decide what to do with a classified ticket. Pure: same input, same output.

    Policy: human review wins over everything, including urgency. A critical
    ticket at 0.5 confidence is still escalated - the system must not act hardest
    exactly where it is least certain. The cost is a slower path for genuine
    incidents that happen to classify badly; that is the right trade for an
    internal inbox where a human is minutes away, and it is a deliberate choice
    rather than an oversight.

    Escalated tickets are still filed, so nothing is ever lost - they are simply
    labelled for review instead of flowing to the owning team.
    """
    issue_type, category_labels = CATEGORY_ROUTING[result.category]
    priority = URGENCY_TO_PRIORITY[result.urgency]
    reason = needs_review(result)

    # The model returned the requester's own words ("about two days"). Convert
    # them here, in the pure layer, so the Jira client receives a value it can
    # use directly and needs no logic of its own. Returns None when the phrase
    # was not a real duration, and None means the field is simply left unset.
    estimate = normalise_estimate(result.estimate)

    if reason is not None:
        return Action(
            decision=Decision.ESCALATE,
            issue_type="Task",
            # Escalations keep their real urgency: a human should still see the
            # critical ones first in the review queue.
            priority=priority,
            labels=("triage-agent", "needs-review", result.category.value),
            summary=result.summary,
            reason=reason,
            estimate=estimate,
        )

    return Action(
        decision=Decision.AUTO_FILE,
        issue_type=issue_type,
        priority=priority,
        labels=("triage-agent", result.category.value, *category_labels),
        summary=result.summary,
        estimate=estimate,
    )
