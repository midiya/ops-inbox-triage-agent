"""Tests for the estimate normaliser. No network, no API key, no model.

    python tests/test_estimates.py

Every case here is a phrasing a person might actually type. The ones that must
return None matter as much as the ones that must parse: an invented duration is
worse than an absent one, because a number in a Jira field gets trusted and
summed, while an empty field gets asked about.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.estimates import normalise_estimate

results: list[tuple[str, bool]] = []


def case(raw, expected) -> None:
    got = normalise_estimate(raw)
    ok = got == expected
    results.append((f"{raw!r} -> {got!r}", ok))
    mark = "PASS" if ok else "FAIL"
    detail = "" if ok else f"   (expected {expected!r})"
    print(f"{mark}  {str(raw)[:34]:36s} -> {str(got):10s}{detail}")


print("--- plain numeric, unit passed through ---")
# "2 days" stays "2d" rather than becoming "16h": Jira owns the definition of a
# working day, so handing back the user's unit inherits their site setting.
case("2d", "2d")
case("2 days", "2d")
case("3 hours", "3h")
case("30 minutes", "30m")
case("30min", "30m")
case("1 week", "5d")

print("\n--- worded numbers ---")
case("two days", "2d")
case("about two days", "2d")
case("an hour", "1h")
case("a day", "1d")
case("a couple of hours", "2h")
case("a few days", "3d")

print("\n--- fractions: the one place we must convert ---")
case("half a day", "4h")
case("half an hour", "30m")
case("1.5 hours", "1h 30m")
case("2.5 days", "2d 4h")

print("\n--- Persian ---")
case("نیم ساعت", "30m")
case("دو روز", "2d")
case("سه ساعت", "3h")
case("یک هفته", "5d")
case("حدود دو روز", "2d")
case("۲ روز", "2d")          # Persian-Indic digits
case("۴۵ دقیقه", "45m")

print("\n--- must refuse: not durations ---")
case(None, None)
case("", None)
case("ASAP", None)
case("soon", None)
case("a while", None)
case("as fast as possible", None)
case("فوری", None)
case("by Sunday", None)       # a deadline, not an estimate
case("next Friday", None)

print("\n--- must refuse: nonsense and out of range ---")
case("0 days", None)
case("2000 days", None)       # far past the sanity ceiling: likely a misparse
case("banana", None)
case("12345", None)           # a number with no unit is not a duration

print("\n--- compound ---")
case("1 day 4 hours", "1d 4h")
case("2 hours 30 minutes", "2h 30m")

print("\n--- regressions found by probing, not by writing tests first ---")
# Each of these was wrong or silently empty on the first implementation. They
# are kept as tests because they are ordinary phrasings, not exotic ones - the
# kind that would have failed quietly in front of a real user.

# Was "4h": the leading "two" was dropped and only the "half" survived, so a
# two-and-a-half-day job was booked as half a day.
case("two and a half days", "2d 4h")
case("1 and a half hours", "1h 30m")
case("دو و نیم روز", "2d 4h")

# Were None: the scan hit an unknown word between quantity and unit and gave up.
case("3 business days", "3d")
case("2 working days", "2d")
case("2 روز کاری", "2d")

# Ranges. Deliberately the upper bound: an estimate that turns out short is the
# failure people actually suffer, and the raw phrasing still reaches a human.
case("1-2 days", "2d")
case("2 or 3 hours", "3h")

print("\n--- phrasings that should keep working ---")
case("takes about 3 h", "3h")
case("  2 DAYS  ", "2d")
case("90 minutes", "1h 30m")
case("نیم روز", "4h")
case("چند ساعت", "3h")


failed = sum(1 for _, ok in results if not ok)
print(f"\n{len(results) - failed}/{len(results)} passed.")
sys.exit(1 if failed else 0)
