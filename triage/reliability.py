"""Bounded retry, backoff with jitter, a shared rate-limit gate, and a
dead-letter queue.

This is the module that answers "what happens when things break". Four rules
drive all of it:

  1. **Bounded, never forever.** "Resend until we get an answer" is an infinite
     loop that bills you. Every retry sequence has a hard attempt limit.
  2. **Only retry what can succeed.** A 400 or a refusal fails identically on
     every attempt, so retrying it spends the whole budget to be told no again.
     The error carries `retryable`; this module only reads it.
  3. **Prefer the server's answer to our guess.** Exponential backoff is what you
     do with no information. A `Retry-After` header is information.
  4. **Nothing is ever silently dropped.** A ticket that exhausts its retries
     goes to the dead-letter queue with the reason, so it can be replayed once
     the cause is fixed.

`sleep`, the jitter source, and the clock are all injectable. That is not
decoration - it is what makes retry logic testable. Tests pass a fake sleep that
records delays instead of waiting, so a suite covering multi-attempt backoff
sequences runs in microseconds.
"""

import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_ATTEMPTS = 3        # total tries, not retries-after-the-first
BASE_DELAY = 0.5        # seconds before the first retry
BACKOFF_FACTOR = 2.0    # each wait doubles
MAX_DELAY = 8.0         # ceiling on our *own* computed backoff

# Ceiling on how long we will honour a server's Retry-After. Deliberately much
# larger than MAX_DELAY: waiting is productive when a rate limit is shared across
# the whole run, because the wait fixes the condition for every later ticket too,
# not just this one. Beyond this, the server has effectively said "come back
# later" and blocking the batch is worse than replaying from the dead-letter
# queue.
MAX_RETRY_AFTER_WAIT = 60.0

DEAD_LETTER_PATH = Path(__file__).resolve().parent.parent / "dead_letter.jsonl"
PROCESSED_PATH = Path(__file__).resolve().parent.parent / "processed.jsonl"


def _full_jitter(delay: float) -> float:
    return random.uniform(0, delay)


def backoff_delay(
    attempt: int,
    *,
    base: float = BASE_DELAY,
    factor: float = BACKOFF_FACTOR,
    cap: float = MAX_DELAY,
    jitter: Callable[[float], float] = _full_jitter,
) -> float:
    """Seconds to wait before retrying, for a 1-based attempt number.

    Exponential: 0.5, 1.0, 2.0, 4.0 ... capped at `cap`.

    Then **full jitter** - a uniform value between 0 and the computed delay. This
    is the part people leave out, and it matters: without jitter, every client
    that failed at the same instant retries at the same instant, so the retry
    storm reproduces the overload that caused the failure. Randomising is what
    stops a recovering service being knocked over again by its own clients.
    """
    raw = min(base * (factor ** (attempt - 1)), cap)
    return jitter(raw)


class RateLimitGate:
    """Shared "do not send before" clock for a whole run.

    Exists because of a mismatch that per-call retry logic cannot see: a retry
    budget is per call, but **a rate limit is a property of the account, shared by
    every ticket in the batch.**

    Without this, a 429 on ticket 3 teaches ticket 3 to wait, then ticket 4 fires
    immediately, gets the same 429, burns its own attempts, and so on - one
    throttle turns into a whole-batch failure, with every ticket independently
    rediscovering the same fact.

    With it, the first 429 records when the window reopens and every later ticket
    waits for it instead of guessing. The batch pauses once, not once per ticket.

    A lightweight circuit breaker. A production version would share this across
    processes (Redis, or the queue's own visibility timeout); in one process an
    instance is enough.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        # monotonic, not wall-clock: immune to the system clock being adjusted
        # mid-run, which would otherwise make a wait finish early or never.
        self._clock = clock
        self._not_before = 0.0

    def note_retry_after(self, seconds: float) -> None:
        """Record that nothing should be sent for `seconds` from now.

        `max` rather than assignment: a later, shorter hint must not shorten a
        longer wait we already know about.
        """
        self._not_before = max(self._not_before, self._clock() + seconds)

    def remaining(self) -> float:
        """Seconds still to wait before the next call is allowed. 0 if clear."""
        return max(0.0, self._not_before - self._clock())

    def wait_if_needed(self, sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until the window reopens. Returns how long it waited."""
        delay = self.remaining()
        if delay > 0:
            logger.info(
                "Rate-limit gate is closed; waiting %.1fs before the next call.",
                delay,
            )
            sleep(delay)
        return delay


def with_retry(
    fn: Callable[[], T],
    *,
    description: str,
    attempts: int = MAX_ATTEMPTS,
    gate: RateLimitGate | None = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float], float] = _full_jitter,
) -> T:
    """Call `fn`, retrying only failures whose `.retryable` is True.

    Re-raises the final exception rather than returning a sentinel: the caller
    needs to know it failed, and needs the reason for the dead-letter record.
    Returning None here would be the "empty object" mistake in another costume.

    Anything without a `retryable` attribute is treated as not retryable - the
    safe default, because an unexpected exception is probably a bug in our own
    code and retrying a bug just runs it three times.
    """
    for attempt in range(1, attempts + 1):
        if gate is not None:
            gate.wait_if_needed(sleep)

        try:
            return fn()
        except Exception as e:
            retryable = getattr(e, "retryable", False)
            retry_after = getattr(e, "retry_after", None)

            if not retryable:
                logger.error(
                    "%s failed permanently on attempt %d/%d (%s): %s",
                    description, attempt, attempts, type(e).__name__, e,
                )
                raise

            # The server told us when it will be ready. Record it on the shared
            # gate so every other ticket in this run benefits, not just this one.
            if retry_after is not None and gate is not None:
                gate.note_retry_after(retry_after)

            if retry_after is not None and retry_after > MAX_RETRY_AFTER_WAIT:
                # Honouring this would block the batch for longer than the work
                # is worth; retrying sooner would just earn another 429. So stop
                # and let the caller dead-letter it for replay - the honest
                # option, rather than pretending either extreme is fine.
                logger.error(
                    "%s: server asked for %.0fs, above our %.0fs ceiling; "
                    "giving up so it can be replayed later.",
                    description, retry_after, MAX_RETRY_AFTER_WAIT,
                )
                raise

            if attempt == attempts:
                logger.error(
                    "%s exhausted all %d attempts (%s): %s",
                    description, attempts, type(e).__name__, e,
                )
                raise

            # Server's number beats ours whenever we have it.
            delay = (
                retry_after if retry_after is not None
                else backoff_delay(attempt, jitter=jitter)
            )
            logger.warning(
                "%s failed on attempt %d/%d (%s); retrying in %.2fs%s",
                description, attempt, attempts, type(e).__name__, delay,
                " (server-specified)" if retry_after is not None else "",
            )
            sleep(delay)

    # Unreachable: the loop either returns or raises. Kept explicit so a future
    # edit that breaks the invariant fails loudly instead of returning None.
    raise AssertionError("with_retry exited its loop without returning or raising")


def dead_letter(
    ticket_id: str,
    ticket_text: str,
    error: BaseException,
    *,
    path: Path = DEAD_LETTER_PATH,
) -> None:
    """Append a permanently-failed ticket to the dead-letter queue.

    Append-only JSONL rather than a database: it survives a crash, it is
    trivially greppable, and replaying it is a loop over a file. The point is
    that a failure leaves a record a human can act on - a dropped ticket that
    only ever appeared in a log line is a dropped ticket.

    The original text is stored, not just the id, so the queue is replayable on
    its own without the source system still having the ticket.
    """
    record = {
        "ticket_id": ticket_id,
        "ticket_text": ticket_text,
        "error_type": type(error).__name__,
        "error": str(error)[:1000],
        "status_code": getattr(error, "status_code", None),
        "retryable": getattr(error, "retryable", None),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.error("Ticket %s written to dead-letter queue: %s", ticket_id, path.name)


def load_processed(path: Path = PROCESSED_PATH) -> dict[str, str]:
    """Map of ticket_id -> issue key for everything already filed.

    This is the idempotency store, and the reason a retry cannot file the same
    ticket twice: the guard is checked before the call, the record written after
    it succeeds.

    Honest limitation, worth saying out loud rather than being caught on: it is
    local state. Lose the file and you lose the guard. A production version would
    query Jira for the `src-<id>` label, or keep this in a database with a unique
    constraint on ticket_id - which is the only version that is actually safe
    with two workers running at once.
    """
    if not path.exists():
        return {}

    processed: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                processed[record["ticket_id"]] = record["issue_key"]
            except (json.JSONDecodeError, KeyError):
                logger.warning("Skipping malformed processed record: %s", line[:80])
    return processed


def mark_processed(
    ticket_id: str, issue_key: str, *, path: Path = PROCESSED_PATH
) -> None:
    """Record that `ticket_id` produced `issue_key`. Written only after success."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ticket_id": ticket_id, "issue_key": issue_key}) + "\n")
