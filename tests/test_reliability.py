"""Tests for the retry policy. No network, no API key, no real waiting.

    py tests/test_reliability.py

Every test here injects a fake `sleep` that records its argument instead of
blocking, and a fake jitter that returns the delay unchanged. That is the whole
trick that makes retry logic testable: a suite covering multi-attempt backoff
sequences with 45-second server hints runs in microseconds. Left with the real
`time.sleep`, these same tests would take about a minute and nobody would run
them.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.exceptions import APICallError, ClassificationFailedError
from triage.reliability import (
    MAX_ATTEMPTS,
    MAX_DELAY,
    MAX_RETRY_AFTER_WAIT,
    RateLimitGate,
    backoff_delay,
    dead_letter,
    load_processed,
    mark_processed,
    with_retry,
)

no_jitter = lambda d: d          # make delays deterministic for assertions
results: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    results.append((label, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


class Recorder:
    """A fake sleep that records instead of waiting, plus a fake clock driven by
    those same recorded sleeps - so the gate behaves as if time really passed."""

    def __init__(self):
        self.delays: list[float] = []
        self.now = 0.0

    def sleep(self, d: float) -> None:
        self.delays.append(d)
        self.now += d

    def clock(self) -> float:
        return self.now


# --- backoff_delay -------------------------------------------------------
print("--- backoff_delay ---")
raw = [backoff_delay(a, jitter=no_jitter) for a in range(1, 7)]
check(f"exponential and capped: {raw}",
      raw == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0])
check("never exceeds MAX_DELAY",
      all(backoff_delay(a, jitter=no_jitter) <= MAX_DELAY for a in range(1, 20)))
# Full jitter must be able to return less than the raw delay, or it is not
# jittering - that is the property, not the specific value.
samples = [backoff_delay(3) for _ in range(50)]
check("full jitter stays within [0, raw]", all(0 <= s <= 2.0 for s in samples))
check("full jitter actually varies", len(set(samples)) > 1)


# --- with_retry: what gets retried ---------------------------------------
print("\n--- with_retry ---")

calls = {"n": 0}


def flaky_then_ok():
    calls["n"] += 1
    if calls["n"] < 3:
        raise APICallError("503", retryable=True, status_code=503)
    return "ok"


rec = Recorder()
out = with_retry(flaky_then_ok, description="flaky", sleep=rec.sleep, jitter=no_jitter)
check("retries a retryable error and returns the eventual success",
      out == "ok" and calls["n"] == 3)
check(f"waited between attempts: {rec.delays}", rec.delays == [0.5, 1.0])

# The rule that matters most, pinned down: a terminal error must never be retried.
calls["n"] = 0


def always_400():
    calls["n"] += 1
    raise APICallError("400", retryable=False, status_code=400)


rec = Recorder()
try:
    with_retry(always_400, description="terminal", sleep=rec.sleep, jitter=no_jitter)
    check("terminal error raises", False)
except APICallError:
    check("terminal error raises immediately", True)
check("terminal error is tried exactly once", calls["n"] == 1)
check("terminal error never sleeps", rec.delays == [])

# ClassificationFailedError inherits retryable=False from TriageError.
calls["n"] = 0


def refusal():
    calls["n"] += 1
    raise ClassificationFailedError("refused", model="gpt-4o-mini")


rec = Recorder()
try:
    with_retry(refusal, description="refusal", sleep=rec.sleep, jitter=no_jitter)
except ClassificationFailedError:
    pass
check("ClassificationFailedError is not retried", calls["n"] == 1)

# Bounded: a permanently-failing retryable error stops at MAX_ATTEMPTS.
calls["n"] = 0


def always_503():
    calls["n"] += 1
    raise APICallError("503", retryable=True, status_code=503)


rec = Recorder()
try:
    with_retry(always_503, description="always down", sleep=rec.sleep, jitter=no_jitter)
except APICallError:
    pass
check(f"bounded at MAX_ATTEMPTS={MAX_ATTEMPTS}", calls["n"] == MAX_ATTEMPTS)
check("sleeps once fewer than attempts", len(rec.delays) == MAX_ATTEMPTS - 1)


# --- retry_after beats computed backoff ----------------------------------
print("\n--- Retry-After ---")
calls["n"] = 0


def throttled_then_ok():
    calls["n"] += 1
    if calls["n"] == 1:
        raise APICallError("429", retryable=True, status_code=429, retry_after=20.0)
    return "ok"


rec = Recorder()
out = with_retry(throttled_then_ok, description="throttled",
                 sleep=rec.sleep, jitter=no_jitter)
check("server hint used instead of 0.5s backoff", rec.delays == [20.0] and out == "ok")

# Above the ceiling: give up now rather than block the batch, so the caller can
# dead-letter it for replay.
calls["n"] = 0


def throttled_long():
    calls["n"] += 1
    raise APICallError("429", retryable=True, status_code=429,
                       retry_after=MAX_RETRY_AFTER_WAIT + 1)


rec = Recorder()
try:
    with_retry(throttled_long, description="long throttle",
               sleep=rec.sleep, jitter=no_jitter)
except APICallError:
    pass
check(f"Retry-After above {MAX_RETRY_AFTER_WAIT:.0f}s gives up immediately",
      calls["n"] == 1 and rec.delays == [])


# --- the shared gate: the batch pauses once, not per ticket ---------------
print("\n--- RateLimitGate ---")
rec = Recorder()
gate = RateLimitGate(clock=rec.clock)

check("gate starts open", gate.remaining() == 0.0)
gate.note_retry_after(20.0)
check("gate closes for 20s", gate.remaining() == 20.0)
gate.note_retry_after(5.0)
check("a shorter later hint does not shorten the wait", gate.remaining() == 20.0)
waited = gate.wait_if_needed(rec.sleep)
check("waiting drains the gate", waited == 20.0 and gate.remaining() == 0.0)

# The scenario that motivated the gate: ticket 1 gets throttled, and ticket 2
# must wait for the shared window instead of firing and being throttled too.
rec = Recorder()
gate = RateLimitGate(clock=rec.clock)
t1 = {"n": 0}


def ticket_one():
    t1["n"] += 1
    if t1["n"] == 1:
        raise APICallError("429", retryable=True, status_code=429, retry_after=20.0)
    return "t1 ok"


with_retry(ticket_one, description="ticket-1", gate=gate,
           sleep=rec.sleep, jitter=no_jitter)
before_ticket_two = len(rec.delays)
with_retry(lambda: "t2 ok", description="ticket-2", gate=gate,
           sleep=rec.sleep, jitter=no_jitter)
# Ticket 1 both waited out its own retry AND left the gate drained, so ticket 2
# sails through without discovering the limit for itself.
check("ticket 2 does not re-discover the rate limit",
      len(rec.delays) == before_ticket_two)


# --- dead-letter queue and idempotency store -----------------------------
print("\n--- DLQ and idempotency ---")
with tempfile.TemporaryDirectory() as tmp:
    dlq = Path(tmp) / "dlq.jsonl"
    err = APICallError("boom", retryable=False, status_code=400)
    dead_letter("T-99", "the original ticket text", err, path=dlq)
    dead_letter("T-98", "another one", err, path=dlq)
    lines = dlq.read_text(encoding="utf-8").strip().splitlines()
    check("DLQ appends one line per failure", len(lines) == 2)
    check("DLQ keeps the original text and the status",
          "the original ticket text" in lines[0] and '"status_code": 400' in lines[0])

    store = Path(tmp) / "processed.jsonl"
    check("empty store loads as empty", load_processed(store) == {})
    mark_processed("T-001", "SCRUM-7", path=store)
    mark_processed("T-002", "SCRUM-8", path=store)
    loaded = load_processed(store)
    check("processed store round-trips",
          loaded == {"T-001": "SCRUM-7", "T-002": "SCRUM-8"})

    # A corrupt line must not cost the other records.
    with store.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    check("a malformed line is skipped, not fatal", len(load_processed(store)) == 2)


failed = sum(1 for _, ok in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed.")
sys.exit(1 if failed else 0)
