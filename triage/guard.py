"""Access control and spend limiting for the HTTP front door.

Two independent protections, because they fail differently:

  * The shared token answers "is this caller allowed to spend my money".
  * The cost guard answers "how much can anyone spend, allowed or not".

The second one matters even when the first is working perfectly. The most
likely way this endpoint ever runs up a bill is not an attacker - it is a bug,
a stuck n8n loop, or a retry storm, all of which arrive holding a valid token.
Authentication alone would let every one of them through.

The limiter takes an injectable clock for the same reason `with_retry` takes an
injectable sleep: a rate limit is only testable if you can move time without
waiting for it.
"""

import logging
import secrets
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)


def token_matches(expected: str, provided: str | None) -> bool:
    """Constant-time comparison of the shared secret.

    `secrets.compare_digest` rather than `==` so the time taken does not depend
    on how many leading characters were correct. Plain equality short-circuits
    at the first difference, which leaks the answer one character at a time to
    anyone who can measure response times precisely enough.

    An empty `expected` never matches. The service is configured to refuse
    requests outright in that case, but the rule is restated here so this
    function cannot be the thing that opens a hole.
    """
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)


class CostGuard:
    """A rolling-window limit on how many calls may be made.

    Two windows over one list of timestamps: a short one to blunt a burst, and a
    long one to bound the worst case over a whole day.

    Rolling rather than calendar-based on purpose. A limit that resets at
    midnight needs a timezone, and invites the failure where an attacker spends
    the daily budget at 23:59 and the whole budget again at 00:01.

    This is per-process and in-memory, so restarting the service clears it. That
    is a real limitation and it is why the OpenAI project budget matters more
    than this does: this guard bounds a mistake, the billing cap bounds the
    damage.
    """

    def __init__(
        self,
        per_minute: int,
        per_day: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._calls: deque[float] = deque()

    def _prune(self, now: float) -> None:
        day_ago = now - 86_400
        while self._calls and self._calls[0] < day_ago:
            self._calls.popleft()

    def check(self) -> str | None:
        """Record a call and return None, or return why it was refused.

        Returning a reason string rather than raising keeps this usable from
        anywhere - the HTTP layer turns it into a 429, a CLI could print it.
        Nothing is recorded when the call is refused, so a client hammering a
        closed door cannot extend its own lockout.
        """
        now = self._clock()
        self._prune(now)

        if len(self._calls) >= self.per_day:
            return (
                f"Daily limit reached ({self.per_day} requests in 24h). "
                "Refusing to spend more."
            )

        minute_ago = now - 60
        recent = sum(1 for t in self._calls if t >= minute_ago)
        if recent >= self.per_minute:
            return (
                f"Rate limit reached ({self.per_minute} requests/minute). "
                "Try again shortly."
            )

        self._calls.append(now)
        return None

    def snapshot(self) -> dict[str, int]:
        """Current usage, for the health endpoint. Does not count as a call."""
        now = self._clock()
        self._prune(now)
        minute_ago = now - 60
        return {
            "last_minute": sum(1 for t in self._calls if t >= minute_ago),
            "last_day": len(self._calls),
            "per_minute_limit": self.per_minute,
            "per_day_limit": self.per_day,
        }
