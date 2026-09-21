"""Files an Action as a Jira issue.

This module is an I/O shell: it makes no decisions. The router already decided
the issue type, the priority and the labels; this just builds a payload and posts
it. That is why it can stay short enough to be obviously correct - all the
judgment that could be wrong lives in pure, tested functions elsewhere.

Two things here are worth reading rather than skimming:

  * `build_issue_payload` is a pure function, so the exact JSON sent to Jira is
    testable offline with no credentials.
  * The error mapping mirrors triage.errors: transient vs permanent, decided by
    HTTP status, so the retry layer treats a Jira failure and a classification
    failure identically and needs to know nothing about either.
"""

import json
import logging
from dataclasses import dataclass

import httpx

from triage.config import (
    JIRA_API_TOKEN,
    JIRA_BASE_URL,
    JIRA_CONFIGURED,
    JIRA_EMAIL,
    JIRA_PROJECT_KEY,
    JIRA_SUPPORTS_TIMETRACKING,
    JIRA_TIMEOUT,
)
from triage.exceptions import APICallError, parse_retry_after
from triage.router import Action

logger = logging.getLogger(__name__)


class JiraError(APICallError):
    """A Jira call failed.

    A marker subclass, not a new taxonomy: it inherits per-instance `retryable`,
    `status_code` and `retry_after` from APICallError, so the retry layer handles
    a Jira failure and an OpenAI failure with exactly the same code. The subclass
    exists only so a caller that cares specifically about Jira can catch it.

    Defining a parallel Transient/Permanent pair here would have been the same
    duplication as the deleted errors.py, one layer down.
    """


@dataclass(frozen=True)
class FiledIssue:
    """The outcome of filing. `mocked` is part of the record on purpose: a demo
    should never be able to pass a fake result off as a real one."""

    key: str
    url: str
    mocked: bool = False


def browse_url(key: str) -> str:
    """The human-facing URL for an issue key.

    Split out so the skipped-duplicate path in the pipeline can rebuild a link
    from a stored key without re-deriving the format - one definition, one place
    to fix if Jira ever changes it.
    """
    return f"{JIRA_BASE_URL}/browse/{key}"


def _adf(text: str) -> dict:
    """Wrap plain text as an Atlassian Document Format paragraph.

    The v3 API will not accept a plain string for `description`; it wants this
    nested shape. Isolated in its own function so the noise lives in one place
    and the payload builder stays readable.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def build_issue_payload(
    action: Action, ticket_id: str, ticket_text: str
) -> dict:
    """Build the Jira create-issue body. Pure - no network, no config lookups
    beyond the project key, so the exact JSON is testable offline.

    The description carries the original ticket text verbatim. That matters: the
    summary is the model's paraphrase, and anyone working the ticket needs the
    words the requester actually wrote. We never replace the source with our
    interpretation of it.
    """
    lines = [
        f"Source ticket: {ticket_id}",
        f"Triaged automatically by ops-inbox-triage-agent.",
        "",
        "--- original ticket ---",
        ticket_text,
    ]
    if action.estimate:
        # Written into the description as well as the field. The description
        # always works; the field depends on project settings. Duplicating one
        # short line is cheaper than an estimate that silently disappears
        # because time tracking happened to be switched off.
        lines += ["", f"Stated estimate: {action.estimate}"]

    if action.reason:
        lines += ["", f"--- flagged for review ---", action.reason]

    fields: dict = {
        "project": {"key": JIRA_PROJECT_KEY},
        "issuetype": {"name": action.issue_type},
        "summary": action.summary[:255],  # Jira rejects summaries over 255 chars
        "description": _adf("\n".join(lines)),
        # The source label is what makes a filed ticket traceable back to its
        # origin, and what a search-based idempotency check would key on.
        "labels": [*action.labels, f"src-{ticket_id}"],
    }

    if action.priority:
        fields["priority"] = {"name": action.priority}

    if action.estimate and JIRA_SUPPORTS_TIMETRACKING:
        # Only when the project really has the field. Jira rejects the whole
        # request if it does not, so an unchecked extra field would turn a
        # working pipeline into one that files nothing at all.
        fields["timetracking"] = {"originalEstimate": action.estimate}

    return {"fields": fields}


def file_issue(
    action: Action, ticket_id: str, ticket_text: str, *, mock: bool = False
) -> FiledIssue:
    """Create the Jira issue and return its key and browse URL.

    `mock=True` returns a plausible fake without touching the network, so the
    whole pipeline is demonstrable with no credentials and no side effects. The
    payload is still built in mock mode - that way the mock path exercises the
    same code as the real one, instead of being a separate branch that silently
    rots.
    """
    payload = build_issue_payload(action, ticket_id, ticket_text)

    if mock:
        fake_key = f"{JIRA_PROJECT_KEY or 'MOCK'}-MOCK"
        logger.info(
            "MOCK: would create %s issue %s priority=%s labels=%s",
            action.issue_type,
            fake_key,
            action.priority,
            payload["fields"]["labels"],
        )
        return FiledIssue(key=fake_key, url="(mock, not filed)", mocked=True)

    if not JIRA_CONFIGURED:
        # Not retryable: no amount of retrying invents credentials.
        raise JiraError(
            "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, "
            "JIRA_API_TOKEN and JIRA_PROJECT_KEY in .env, or run with mock=True.",
            retryable=False,
        )

    url = f"{JIRA_BASE_URL}/rest/api/3/issue"
    try:
        response = httpx.post(
            url,
            json=payload,
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=JIRA_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except httpx.TimeoutException as e:
        raise JiraError(
            f"Jira timed out after {JIRA_TIMEOUT}s: {e}", retryable=True
        ) from e
    except httpx.RequestError as e:
        # No response at all - DNS, TLS, socket. Nothing was rejected, so there
        # is no status code to record.
        raise JiraError(f"Could not reach Jira: {e}", retryable=True) from e

    if response.status_code >= 400:
        # Same rule as the OpenAI mapping, deliberately identical: 429 and 5xx
        # are their side and worth retrying; every other 4xx is our request and
        # will fail identically however many times we send it.
        code = response.status_code
        raise JiraError(
            f"Jira returned HTTP {code}: {response.text[:400]}",
            retryable=code == 429 or code >= 500,
            status_code=code,
            retry_after=parse_retry_after(response.headers.get("retry-after")),
        )

    body = response.json()
    key = body["key"]
    return FiledIssue(key=key, url=browse_url(key), mocked=False)
