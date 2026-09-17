"""Resolve a follow-up message against the products shown in the last turn.

The assistant ends every recommendation with "Reply with an item number", so
"2" has to mean the second item rather than a fresh search for the string "2".

The whole message must match a selector pattern, never just contain one.
Substring matching would wreck ordinary queries: "2 piece kurta set" and
"under 3000" both contain digits but are searches, not selections.
"""

import re
from typing import Optional

ORDINAL_WORDS = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1,
}

# Optional lead-ins people actually type before naming an item
_LEAD_IN = r"(?:(?:i(?:'?ll| will| would like to)? ?(?:take|want|like)|show me|tell me (?:more )?about|send me|give me|details? (?:of|for)|more (?:on|about))\s+)?"
_ARTICLE = r"(?:the\s+)?"
_SUFFIX = r"(?:\s+one)?(?:\s+please)?"

_NUMERIC = re.compile(
    rf"^{_LEAD_IN}{_ARTICLE}(?:item|option|number|no\.?|#)?\s*(\d{{1,2}}){_SUFFIX}$"
)
_ORDINAL = re.compile(
    rf"^{_LEAD_IN}{_ARTICLE}({'|'.join(ORDINAL_WORDS)})(?:\s+(?:item|option|one))?{_SUFFIX}$"
)


def parse_selection(text: str, count: int) -> Optional[int]:
    """Return a zero-based index into the last shown products, or None.

    None means "not a selection" — the caller should treat the message as a
    new search. Out-of-range numbers return None on purpose: "under 3000" with
    three products shown is a budget, not a choice.
    """
    if count <= 0:
        return None

    cleaned = text.strip().lower().rstrip(".!?").strip()
    if not cleaned:
        return None

    match = _NUMERIC.match(cleaned)
    if match:
        position = int(match.group(1))
        return position - 1 if 1 <= position <= count else None

    match = _ORDINAL.match(cleaned)
    if match:
        position = ORDINAL_WORDS[match.group(1)]
        if position == -1:
            return count - 1
        return position - 1 if position <= count else None

    return None


def describe_choice(product: dict, index: int) -> str:
    """Confirm the pick and invite the next step."""
    name = product.get("name", "That item")
    price = product.get("price_inr", product.get("price", 0))
    description = product.get("rich_description") or product.get("description") or ""

    lines = [f"Good choice — {name}, Rs. {price:,.0f}."]
    if description:
        lines.append(description)
    lines.append("Reply BUY to place this order, or ask to see other options.")
    return "\n".join(lines)
