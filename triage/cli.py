"""Runner for the triage pipeline.

    py -m triage.cli "some ticket text"    one ticket, mock mode
    py -m triage.cli --all                every sample ticket, mock mode
    py -m triage.cli --all --file          ...and actually create Jira issues
    py -m triage.cli --all -v              show retry / escalation logging

**Mock is the default and `--file` is opt-in.** A tool whose default action
creates real records in a real system is a tool you cannot re-run while
debugging, and one stray Enter mid-demo would put twelve issues on the board.

samples/tickets.jsonl is the eval set: a fixed set of inputs, including the
deliberately nasty ones, so after any change to the schema or the instructions
you can re-run the same tickets and see what moved. Without a fixed set, tuning
a prompt is guessing.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from triage.estimates import normalise_estimate
from triage.pipeline import Outcome, process_batch, process_ticket

logger = logging.getLogger(__name__)

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "tickets.jsonl"
# Resolved from this file, not the working directory, so the CLI runs from
# anywhere - the same lesson as the config import.


def load_samples(path: Path = SAMPLES) -> list[dict]:
    """Read the JSONL eval set. Skips blank lines; reports bad ones by number."""
    tickets = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                tickets.append(json.loads(line))
            except json.JSONDecodeError as e:
                # Report and continue: one malformed line must not cost you the
                # other eleven tickets.
                logger.error("samples line %d is not valid JSON: %s", lineno, e)
    return tickets


def print_outcome(outcome: Outcome, text: str) -> None:
    head = text[:66].replace("\n", " ")
    print(f"[{outcome.ticket_id}] {head}{'...' if len(text) > 66 else ''}")

    if outcome.status == "skipped":
        print("    SKIPPED - already filed (idempotency guard)\n")
        return

    if outcome.status == "failed":
        e = outcome.error
        print(f"    FAILED  {type(e).__name__}: {str(e)[:100]}")
        print(f"    retryable={getattr(e, 'retryable', 'n/a')} -> dead_letter.jsonl\n")
        return

    r, a, issue = outcome.result, outcome.action, outcome.issue
    flag = "  <-- NEEDS HUMAN" if a.decision.name == "ESCALATE" else ""
    print(f"    {r.category.value:24s} {r.urgency.value:9s} conf={r.confidence}{flag}")
    print(f"    summary: {r.summary}")
    if r.affected_system or r.deadline or r.estimate:
        print(
            f"    system={r.affected_system!r}  deadline={r.deadline!r}  "
            f"estimate={r.estimate!r} -> {normalise_estimate(r.estimate)!r}"
        )
    if a.reason:
        print(f"    escalate: {a.reason}")
    print(
        f"    -> {a.decision.value:9s} {a.issue_type:16s} {a.priority:8s} "
        f"labels={list(a.labels)}"
    )
    print(f"    -> {issue.key}  {issue.url}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify and file ops inbox tickets.")
    parser.add_argument("ticket", nargs="?", help="ticket text to classify")
    parser.add_argument("--all", action="store_true", help="run the whole sample set")
    parser.add_argument(
        "--file",
        action="store_true",
        help="actually create Jira issues (default is mock, nothing is filed)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show logs")
    args = parser.parse_args()

    # Logging configured here, in the entry point - never in a library module. A
    # library that calls basicConfig() hijacks logging for every application that
    # imports it.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    mock = not args.file
    banner = "MOCK - nothing will be filed" if mock else "LIVE - creating real Jira issues"
    print(f"[{banner}]\n")

    if args.all:
        tickets = load_samples()
        print(f"Loaded {len(tickets)} sample tickets\n")
        outcomes = process_batch(tickets, mock=mock)

        by_id = {t["id"]: t["text"] for t in tickets}
        for o in outcomes:
            print_outcome(o, by_id[o.ticket_id])

        counts = {s: sum(1 for o in outcomes if o.status == s)
                  for s in ("filed", "skipped", "failed")}
        escalated = sum(
            1 for o in outcomes
            if o.action is not None and o.action.decision.name == "ESCALATE"
        )
        print(
            f"Done. {counts['filed']} filed, {counts['skipped']} skipped, "
            f"{counts['failed']} failed, {escalated} needed human review."
        )
        return 1 if counts["failed"] else 0

    if not args.ticket:
        parser.error("give a ticket text, or --all")

    outcome = process_ticket("ad-hoc", args.ticket, mock=mock)
    print_outcome(outcome, args.ticket)
    return 1 if outcome.status == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
