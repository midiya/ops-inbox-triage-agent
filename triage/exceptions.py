"""Exception vocabulary for the triage package.

Every failure raised here inherits from `TriageError`, so a caller can catch a
single class and still ask `err.retryable` to decide what happens next.

Deliberately free of vendor imports: these are domain failures. Translating a
provider SDK's errors into them belongs next to the code that talks to that
provider, so adding a second model provider never touches this file.
"""

from email.utils import parsedate_to_datetime


def parse_retry_after(value: str | None) -> float | None:
    """Read a `Retry-After` header into seconds, or None if unusable.

    Lives here because its only job is to populate `APICallError.retry_after`,
    and both the OpenAI and the Jira layer need it - one copy, so the date-format
    edge case below is fixed in one place.

    RFC 9110 allows two forms:
      * delay-seconds - `Retry-After: 20`
      * an HTTP-date  - `Retry-After: Wed, 21 Oct 2015 07:28:00 GMT`

    Both are handled. The date form is converted to a delay by subtracting now,
    which is why this function is not quite pure - it reads the clock. That is
    unavoidable: an absolute deadline can only become a duration relative to a
    present moment.

    Never raises. A malformed header is a reason to fall back to computed
    backoff, not a reason to lose the ticket - the caller is already handling a
    failure and must not be handed a second one from its own error path.
    """
    if not value:
        return None

    value = value.strip()

    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        # Negative or absurd values are ignored rather than trusted: a server
        # asking us to wait an hour has effectively failed the request, and our
        # bounded retry budget should decide that, not the header.
        return seconds if 0 <= seconds <= 300 else None

    try:
        from datetime import datetime, timezone

        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return None

    return delta if 0 <= delta <= 300 else None


class TriageError(Exception):
    """Base for every failure this package raises.

    Exists so a caller can choose its altitude: `except TriageError` to handle
    anything from triage, or a specific subclass for one failure mode. Without
    it, callers must enumerate our exceptions and silently miss any we add.
    """

    # Class-level default, overridable per instance. Lets a caller ask
    # `err.retryable` of ANY triage error without an isinstance chain.
    retryable: bool = False


class TicketTooLargeError(TriageError, ValueError):
    """The input is too large to be a genuine ticket; refuse rather than guess.

    Also a `ValueError` because the caller's argument really is the problem: a
    ticket is a str, so the type is right and only the value is unacceptable.
    Terminal by definition - the same oversized ticket fails identically.
    """

    def __init__(self, length: int, limit: int):
        # Kept as attributes, not just interpolated into the message, so a
        # handler can log them as structured fields instead of regexing prose.
        self.length = length
        self.limit = limit
        super().__init__(
            f"Ticket is {length} characters; the limit is {limit}. "
            "This is almost certainly a pasted thread or log dump, not a request."
        )


class ClassificationFailedError(TriageError, RuntimeError):
    """The model returned something we could not turn into a TriageResult.

    Deliberately *not* a `ValueError`: the caller's ticket was well-formed and
    accepted; what failed is an external dependency, mid-call. `ValueError`
    would point the finger at the argument and send whoever debugs this to the
    wrong end of the pipeline. `RuntimeError` - "went wrong, no better category
    fits" - is honest, and keeps compatibility with callers already catching
    the bare RuntimeError this replaced.

    Terminal on purpose. A re-roll at temperature 0.1 might well succeed, but
    retrying would mask prompt regression - the exact signal worth watching.
    """

    def __init__(self, detail: str, model: str | None = None):
        self.detail = detail
        self.model = model
        super().__init__(f"{detail} (model={model})" if model else detail)


class APICallError(TriageError):
    """The provider call failed before we got a usable response.

    Retryability is per-instance rather than per-class because 429 and 401 are
    the same kind of event - the call failed - with opposite correct responses.
    """

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        model: str | None = None,
    ):
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after   # honour the server's hint over our backoff
        self.model = model
        super().__init__(detail)
