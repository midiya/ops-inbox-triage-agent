"""Tests for the access token check and the spend limiter. Fully offline.

    python tests/test_guard.py

The limiter takes an injected clock, so a test covering a full 24-hour window
runs instantly instead of taking a day. Same trick as the injected sleep in the
retry tests: time is a dependency, so make it one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.guard import CostGuard, token_matches

results: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    results.append((label, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


print("--- token_matches ---")
check("correct token matches", token_matches("secret", "secret"))
check("wrong token rejected", not token_matches("secret", "wrong"))
check("missing token rejected", not token_matches("secret", None))
check("empty provided rejected", not token_matches("secret", ""))
# The important one: an unconfigured server must not accept everything. This is
# the failure mode where a missing env var silently opens the endpoint.
check("empty expected never matches", not token_matches("", ""))
check("empty expected rejects any value", not token_matches("", "anything"))
# Same length, one character different - the case a naive startswith would pass.
check("near-miss rejected", not token_matches("secret", "secres"))


print("\n--- CostGuard: per-minute window ---")
clock = FakeClock()
g = CostGuard(per_minute=3, per_day=100, clock=clock)

check("first three allowed", all(g.check() is None for _ in range(3)))
refusal = g.check()
check("fourth refused", refusal is not None and "minute" in refusal)

# A refused call must not be recorded, or a client hammering a closed door would
# keep extending its own lockout.
check("refusal is not counted", g.snapshot()["last_minute"] == 3)

clock.advance(61)
check("allowed again after the window slides", g.check() is None)


print("\n--- CostGuard: rolling, not calendar ---")
clock = FakeClock()
g = CostGuard(per_minute=100, per_day=5, clock=clock)

for _ in range(5):
    g.check()
check("daily limit reached", (r := g.check()) is not None and "Daily" in r)

# 23 hours later the window has not yet released the early calls. A midnight
# reset would have allowed a full second budget by now.
clock.advance(23 * 3600)
check("still refused 23h later", g.check() is not None)

clock.advance(2 * 3600)   # now 25h after the first calls
check("allowed once the oldest calls age out", g.check() is None)


print("\n--- CostGuard: snapshot does not consume budget ---")
clock = FakeClock()
g = CostGuard(per_minute=2, per_day=10, clock=clock)
g.check()
before = g.snapshot()
g.snapshot()
g.snapshot()
check("snapshot is read-only", g.snapshot()["last_minute"] == before["last_minute"])
check("budget still available", g.check() is None)

print("\n--- CostGuard: the day limit wins over the minute limit ---")
clock = FakeClock()
g = CostGuard(per_minute=10, per_day=2, clock=clock)
g.check()
g.check()
r = g.check()
check("daily refusal reported, not the minute one", r is not None and "Daily" in r)


failed = sum(1 for _, ok in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed.")
sys.exit(1 if failed else 0)
