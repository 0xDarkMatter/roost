"""Externalised keyword lists for 429 classification.

Anthropic's rate-limit error messages are not a stable contract. When they
shift, extend these lists rather than editing match statements in taxonomy.py.

All matching is case-insensitive substring matching on the message body.
"""

from __future__ import annotations

# Substrings indicating a 5-hour session-window limit.
SESSION_KEYWORDS: tuple[str, ...] = (
    "session",
    "5-hour",
    "5 hour",
    "hourly",
    "current session",
)

# Substrings indicating a 7-day / weekly plan limit.
WEEKLY_KEYWORDS: tuple[str, ...] = (
    "weekly",
    "week",
    "plan",
    "7-day",
    "7 day",
    "sunday",
    "resets sun",
)


def contains_any(message: str, keywords: tuple[str, ...]) -> bool:
    """Case-insensitive substring match against any keyword."""
    if not message:
        return False
    haystack = message.lower()
    return any(kw in haystack for kw in keywords)
