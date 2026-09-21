"""Tests for the routing policy. No API key, no network, no pytest needed.

    py tests/test_router.py

This file is the payoff for keeping the router pure. Every case below builds a
TriageResult by hand and asserts on the Action - so the entire decision policy
is verified in milliseconds, offline, for free. If this logic lived inside
classify_ticket it could only be tested by making real API calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.router import CONFIDENCE_FLOOR, Decision, needs_review, route
from triage.schemas import Category, TriageResult, Urgency


def make(
    category=Category.ACCESS_REQUEST,
    urgency=Urgency.NORMAL,
    confidence=0.9,
    needs_human=False,
    escalation_reason=None,
    estimate=None,
) -> TriageResult:
    """Build a TriageResult with sensible defaults, overriding one field at a
    time. Each test then states only what it is actually testing."""
    return TriageResult(
        category=category,
        urgency=urgency,
        summary="test summary",
        affected_system=None,
        affected_person=None,
        deadline=None,
        estimate=estimate,
        confidence=confidence,
        needs_human=needs_human,
        escalation_reason=escalation_reason,
    )


CASES: list[tuple[str, TriageResult, Decision]] = [
    # --- the happy path: everything clean, so it files itself ----------------
    ("clean access request",
     make(), Decision.AUTO_FILE),

    ("clean critical bug",
     make(category=Category.BUG_REPORT, urgency=Urgency.CRITICAL),
     Decision.AUTO_FILE),

    # --- each escalation trigger, one at a time -----------------------------
    ("model asked for a human",
     make(needs_human=True, escalation_reason="Two requests in one ticket."),
     Decision.ESCALATE),

    ("confidence below the floor",
     make(confidence=0.5), Decision.ESCALATE),

    ("category is other",
     make(category=Category.OTHER), Decision.ESCALATE),

    # --- the conflict case: the policy decision, made explicit --------------
    # Critical urgency does NOT buy a bypass. This is the case worth being able
    # to defend out loud: the system must not act hardest where it is least sure.
    ("critical but low confidence -> still escalated",
     make(urgency=Urgency.CRITICAL, confidence=0.4), Decision.ESCALATE),

    # --- boundary conditions on the threshold -------------------------------
    # Exactly at the floor must pass, because the check is `<` not `<=`. An
    # off-by-one here would silently escalate a whole confidence band.
    ("exactly at the floor -> auto-filed",
     make(confidence=CONFIDENCE_FLOOR), Decision.AUTO_FILE),

    ("a hair below the floor -> escalated",
     make(confidence=CONFIDENCE_FLOOR - 0.01), Decision.ESCALATE),
]


def main() -> int:
    failures = 0

    for label, result, expected in CASES:
        action = route(result)
        ok = action.decision is expected
        failures += not ok
        print(
            f"{'PASS' if ok else 'FAIL'}  {label:44s} "
            f"-> {action.decision.value:9s} priority={action.priority}"
        )
        if not ok:
            print(f"      expected {expected.value}, got {action.decision.value}")

    # --- properties that must hold regardless of the case -------------------
    print()
    checks = []

    # An escalation without a reason is unactionable for whoever picks it up.
    esc = route(make(confidence=0.3))
    checks.append(("escalation always carries a reason", bool(esc.reason)))
    checks.append(("escalation is labelled needs-review",
                   "needs-review" in esc.labels))

    # An auto-file must not claim a reason it does not have.
    auto = route(make())
    checks.append(("auto-file carries no reason", auto.reason is None))

    # Urgency survives escalation: a human should still see criticals first.
    crit = route(make(urgency=Urgency.CRITICAL, needs_human=True))
    checks.append(("escalated critical keeps Highest priority",
                   crit.priority == "Highest"))

    # Frozen means frozen - the audit trail cannot be edited after the fact.
    try:
        auto.priority = "Low"
        checks.append(("Action is immutable", False))
    except Exception:
        checks.append(("Action is immutable", True))

    # needs_review must prefer the model's own words over a generic message.
    reason = needs_review(make(needs_human=True, escalation_reason="Specific."))
    checks.append(("model's reason preferred over generic",
                   reason == "Specific."))

    # The estimate is converted here, in the pure layer, so the Jira client
    # receives a ready value and needs no logic of its own.
    checks.append(("stated estimate is converted to Jira format",
                   route(make(estimate="about two days")).estimate == "2d"))
    checks.append(("Persian estimate is converted too",
                   route(make(estimate="نیم ساعت")).estimate == "30m"))
    # A phrase that is not a duration must leave the field unset rather than
    # produce a number nobody stated.
    checks.append(("non-duration leaves the estimate unset",
                   route(make(estimate="ASAP")).estimate is None))
    checks.append(("no estimate stated stays None",
                   route(make()).estimate is None))

    for label, ok in checks:
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}")

    total = len(CASES) + len(checks)
    print(f"\n{total - failures}/{total} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
