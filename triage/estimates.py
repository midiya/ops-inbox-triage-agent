"""Turns a stated duration into Jira's time format. Pure, offline, testable.

The model extracts what the requester wrote - "about two days", "نیم ساعت" -
and this converts it. Conversion is arithmetic plus a vocabulary, which is
exactly the kind of work a language model should not be doing: it is
deterministic, it has a right answer, and a wrong answer here would be
schema-valid and silently wrong.

Two rules shape everything below.

**Pass the user's own unit through wherever possible.** "2 days" becomes "2d",
not "16h". Jira decides what a working day is (8 hours by default, but it is a
per-site setting), so handing it "2d" inherits that definition instead of
imposing ours. Only a fraction - "half a day" - forces us to convert, and that
is the one place our 8h/5d assumption can be wrong.

**Refuse rather than guess.** "a while", "ASAP", "soon" are not durations. They
return None, the Jira field stays empty, and the requester's own words still
appear in the description for a human to read. An invented number is worse than
an absent one, because a number gets trusted.
"""

import re
import unicodedata

# Jira's defaults. Stated here rather than buried in the code because they are
# assumptions, not facts: a site can define a working day as 6 or 7.5 hours, and
# if yours does, the two fraction branches below are wrong by that ratio.
HOURS_PER_DAY = 8
DAYS_PER_WEEK = 5
MINUTES_PER_HOUR = 60

# A sanity ceiling. An estimate beyond this on a support ticket is far more
# likely a parsing mistake ("2000 days") than a real intention, and a wrong
# number that large would distort any report built on the field. Refuse instead.
MAX_MINUTES = 90 * HOURS_PER_DAY * MINUTES_PER_HOUR

# Worded numbers. "a"/"an" count as one - "an hour" is a real estimate.
WORD_NUMBERS: dict[str, float] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
    # Vague-but-conventional quantities. "a couple" is two by common usage; "a
    # few" is taken as three. These are the least defensible entries here - they
    # are convention, not meaning - but refusing them would drop a lot of real
    # phrasing, and being wrong by one hour is not the failure mode worth
    # protecting against.
    "couple": 2, "few": 3, "several": 3,
    # Persian
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6,
    "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "چند": 3, "یه": 1,
}

# Unit names to minutes.
UNITS: dict[str, int] = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 60 * HOURS_PER_DAY, "day": 60 * HOURS_PER_DAY,
    "days": 60 * HOURS_PER_DAY,
    "w": 60 * HOURS_PER_DAY * DAYS_PER_WEEK,
    "week": 60 * HOURS_PER_DAY * DAYS_PER_WEEK,
    "weeks": 60 * HOURS_PER_DAY * DAYS_PER_WEEK,
    # Persian
    "دقیقه": 1, "ساعت": 60, "روز": 60 * HOURS_PER_DAY,
    "روزه": 60 * HOURS_PER_DAY, "هفته": 60 * HOURS_PER_DAY * DAYS_PER_WEEK,
}

# Which Jira letter to emit for each unit family, so the user's own unit
# survives the round trip.
UNIT_LETTER: dict[int, str] = {
    1: "m",
    60: "h",
    60 * HOURS_PER_DAY: "d",
    60 * HOURS_PER_DAY * DAYS_PER_WEEK: "w",
}

HALF_WORDS = {"half", "نیم"}

# Words that may sit between a quantity and its unit without changing it.
# "3 business days" and "2 working days" are ordinary phrasings that produced
# nothing at all before these were listed - the scan hit an unknown word and
# gave up, so the estimate was silently dropped rather than reported wrong.
FILLER_WORDS = {
    "a", "an", "of", "or", "and", "about", "around", "approximately",
    "roughly", "maybe", "business", "working", "work", "calendar", "full",
    # Persian: "و" is "and", "حدود"/"تقریبا" are "about".
    "و", "حدود", "تقریبا", "تقریباً", "کاری",
}

# Words that look like an estimate but carry no duration. Listed so the
# behaviour is a deliberate refusal rather than an accident of the regex.
NON_DURATIONS = {
    "asap", "soon", "urgent", "immediately", "quickly", "a while", "whenever",
    "فوری", "زود", "سریع",
}


def _to_ascii_digits(text: str) -> str:
    """Fold Persian and Arabic-Indic digits to ASCII.

    Without this, a Persian ticket saying two hours in Persian numerals never
    matches a digit pattern and silently produces no estimate. `unicodedata`
    rather than a hand-written table so every decimal digit form is covered, not
    just the two we happened to think of.
    """
    return "".join(
        str(unicodedata.decimal(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )


def _format(total_minutes: float) -> str | None:
    """Render minutes as Jira duration, preferring whole units."""
    minutes = int(round(total_minutes))
    if minutes <= 0 or minutes > MAX_MINUTES:
        return None

    parts: list[str] = []
    for size in sorted(UNIT_LETTER, reverse=True):
        # Weeks are deliberately skipped on output: "1w" reads oddly on a support
        # ticket and Jira renders it as 5d anyway. Anything that large is shown
        # in days.
        if size == 60 * HOURS_PER_DAY * DAYS_PER_WEEK:
            continue
        whole, minutes = divmod(minutes, size)
        if whole:
            parts.append(f"{whole}{UNIT_LETTER[size]}")

    return " ".join(parts) if parts else None


def normalise_estimate(raw: str | None) -> str | None:
    """Convert a stated duration to Jira format, or None if there isn't one.

    Returns strings like "2d", "4h", "1d 4h", "30m". Never raises - the caller
    is already handling a ticket and must not be handed a second failure from a
    formatting helper.

    Examples:
        "about two days"   -> "2d"
        "half a day"       -> "4h"
        "1.5 hours"        -> "1h 30m"
        "نیم ساعت"          -> "30m"
        "ASAP"             -> None
    """
    if not raw:
        return None

    text = _to_ascii_digits(raw).lower().strip()
    if not text:
        return None

    if text in NON_DURATIONS:
        return None

    total = 0.0
    found = False

    # Tokenise on anything that is not a word character or a decimal point, so
    # punctuation and the Persian ZWNJ do not weld tokens together.
    tokens = [t for t in re.split(r"[^\w.۰-۹]+", text) if t]

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # A number glued to its unit: "2d", "30min", "2روز".
        glued = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zء-ي؀-ۿ]+)", token)
        if glued and glued.group(2) in UNITS:
            total += float(glued.group(1)) * UNITS[glued.group(2)]
            found = True
            i += 1
            continue

        # Otherwise: a quantity, then look ahead for its unit.
        quantity: float | None = None
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            quantity = float(token)
        elif token in HALF_WORDS:
            quantity = 0.5
        elif token in WORD_NUMBERS:
            quantity = WORD_NUMBERS[token]

        if quantity is None:
            i += 1
            continue

        # Scan forward for the unit, stepping over filler like "a"/"an"/"of".
        # "half a day", "a couple of hours" and "3 business days" all need this.
        j = i + 1
        unit_minutes: int | None = None
        while j < len(tokens) and j <= i + 4:
            nxt = tokens[j]
            if nxt in UNITS:
                unit_minutes = UNITS[nxt]
                break

            # "two and a half days" - the half belongs to the quantity we are
            # already holding. Without this the "two" was dropped entirely and
            # the phrase parsed as half a day: wrong by a factor of five, and
            # wrong in the direction that under-books the work.
            if nxt in HALF_WORDS:
                quantity += 0.5
                j += 1
                continue

            if nxt in FILLER_WORDS:
                j += 1
                continue

            # "a couple of hours": the quantity word follows the article.
            if nxt in WORD_NUMBERS and quantity == 1:
                quantity = WORD_NUMBERS[nxt]
                j += 1
                continue

            # A second bare number before any unit means a range: "1-2 days",
            # "2 or 3 hours". Take the larger. Deliberate, not accidental: an
            # estimate that turns out short is the failure people actually
            # suffer, so the upper bound is the safer of the two, and a human
            # still sees the original phrasing in the description.
            if re.fullmatch(r"\d+(?:\.\d+)?", nxt):
                quantity = max(quantity, float(nxt))
                j += 1
                continue

            break

        if unit_minutes is None:
            i += 1
            continue

        total += quantity * unit_minutes
        found = True
        i = j + 1

    if not found:
        return None

    return _format(total)
