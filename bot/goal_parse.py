"""Read a goal typed as one line, like "Yoga, 3 days" or "Affirmations every day"."""
from __future__ import annotations

import re

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "once": 1, "twice": 2}
NUM = r"(?<!\d)([1-7])(?!\d)|\b(one|two|three|four|five|six|seven|once|twice)\b"
SEP = r"\s*[,\-–—:|/(]*\s*"

# Every-day phrasing at the end of the line.
DAILY = re.compile(SEP + r"\b(every\s*day|everyday|daily|each\s+day|all\s+week|7\s*/\s*7)\b\)?\s*\.?\s*$", re.I)
# A day count at the end: "3", "3 days", "3x", "3 times a week", "3 days per week", "3/wk", "three days".
COUNT = re.compile(
    SEP + r"(?:" + NUM + r")\s*(?:x|times|days?|d)?\s*(?:(?:a|per|/|each|every|in\s+a)\s*)?(?:week|wk)?\s*\)?\s*\.?\s*$",
    re.I)
TRAILING_JUNK = re.compile(r"[\s,\-–—:|/(]+$")
LEADING_I_WILL = re.compile(r"^\s*i\s+will\s+", re.I)


def parse_goal(text: str) -> tuple[str, int] | str:
    """Returns (goal, days per week), or an error message if the days can't be found."""
    raw = (text or "").strip()
    if not raw:
        return "This goal is empty."
    m = DAILY.search(raw)
    if m:
        days, name = 7, raw[:m.start()]
    else:
        m = COUNT.search(raw)
        if not m:
            return "add how many days a week at the end (1–7), like “Yoga, 3 days” or “Journal, every day”."
        token = (m.group(1) or m.group(2)).lower()
        days, name = (int(token) if token.isdigit() else WORDS[token]), raw[:m.start()]
    name = LEADING_I_WILL.sub("", TRAILING_JUNK.sub("", name)).strip()
    name = re.sub(r"\s+on$", "", name, flags=re.I).strip()
    if not name:
        return "the goal itself is missing (just a number was entered)."
    return name[:80], days


def format_goal(name: str, days: int) -> str:
    """How a saved goal is shown back in the form so it can be edited."""
    return f"{name}, every day" if days == 7 else f"{name}, {days} day{'s' if days > 1 else ''}"
