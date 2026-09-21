from dotenv import load_dotenv
import os
import logging
logger = logging.getLogger(__name__)

load_dotenv()

def get_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)

    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        logger.exception(
            f"{name} must be an integer, got: {value!r}"
        )
        raise ValueError(f"{name} must be an integer, got: {value!r}"
)
    

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY or not OPENAI_API_KEY.strip():
    logger.error("OPENAI_API_KEY not set. Copy .env.example to .env and set the key.")
    raise RuntimeError(
        "OPENAI_API_KEY not set. Copy .env.example to .env and set the key."
    )

# Character limits, not token limits - the name says which. A 128k-token context
# window means overflow is not the risk here; these are anomaly bounds, set well
# above real traffic (measured: ~130 chars median) so they only fire on things
# that are not tickets.
SOFT_INPUT_CHARS = get_int_env("SOFT_INPUT_CHARS", 2000)   # classify, but flag
MAX_INPUT_CHARS = get_int_env("MAX_INPUT_CHARS", 8000)     # refuse outright

MAX_OUTPUT_TOKENS = get_int_env("MAX_OUTPUT_TOKENS", 800)


TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "gpt-4o-mini")


# --- Jira ----------------------------------------------------------------
# Deliberately NOT required at import, unlike OPENAI_API_KEY. The classifier and
# router must stay usable with no Jira configured at all - otherwise you cannot
# run the eval set, or the router tests, without credentials for a system they
# never touch. Missing Jira config is reported by the client when someone
# actually tries to file, not by this module when someone imports it.
JIRA_BASE_URL = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")

# Seconds. Short enough that a hung Jira does not stall the batch, long enough
# that a slow-but-working Jira is not treated as a failure.
JIRA_TIMEOUT = get_int_env("JIRA_TIMEOUT", 20)

# Whether the project has time tracking switched on. Off by default, because
# sending `timetracking` to a project that does not have the field returns a 400
# and loses the whole issue - a harmless-looking extra field would break every
# ticket, not just the ones with an estimate.
#
# Check before turning this on: GET /rest/api/3/issue/createmeta must list a
# `timetracking` field for your issue types. Turn it on in Jira under
# Project settings -> Features -> Time tracking.
JIRA_SUPPORTS_TIMETRACKING = (
    os.environ.get("JIRA_SUPPORTS_TIMETRACKING", "").strip().lower()
    in ("1", "true", "yes")
)

JIRA_CONFIGURED = all(
    (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY)
)


# --- HTTP service protection ---------------------------------------------
# A shared secret every caller of /triage must present. There is no default and
# no fallback: if this is unset the service refuses every request rather than
# serving an open endpoint. An endpoint that spends money must not be reachable
# by accident, and "it worked without configuring anything" is exactly how that
# happens.
TRIAGE_SHARED_TOKEN = os.environ.get("TRIAGE_SHARED_TOKEN", "").strip()

# Spend ceiling, independent of who is calling. This exists for the case the
# token does not cover: a stuck retry loop or a bug in our own workflow arrives
# holding a perfectly valid token. Authentication says who may spend; this says
# how much anyone may spend.
TRIAGE_MAX_PER_MINUTE = get_int_env("TRIAGE_MAX_PER_MINUTE", 10)
TRIAGE_MAX_PER_DAY = get_int_env("TRIAGE_MAX_PER_DAY", 200)
