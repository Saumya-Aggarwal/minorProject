"""Delivery address validation, shared by the checkout page and the JSON API.

Server-side on purpose: the browser's required/pattern attributes are a
convenience for the customer, not a check anyone can rely on.
"""

import re
from typing import Any

INDIAN_STATES = [
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
    "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa",
    "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka",
    "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
]

FIELDS = ("name", "phone", "line1", "line2", "city", "state", "pincode")

# Indian PIN codes are six digits and never start with 0
_PINCODE = re.compile(r"^[1-9][0-9]{5}$")
# Indian mobile numbers: ten digits starting 6-9, optionally written with +91 or 0
_MOBILE = re.compile(r"^(?:\+?91|0)?([6-9][0-9]{9})$")


def validate(form: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (clean values, errors by field). Errors empty means valid.

    The phone number is normalised to its ten digits, so "+91 98765 43210" and
    "09876543210" are stored the same way.
    """
    values = {key: " ".join(str(form.get(key) or "").split()) for key in FIELDS}
    errors: dict[str, str] = {}

    if len(values["name"]) < 2:
        errors["name"] = "Enter the name of the person receiving the parcel."
    phone = re.sub(r"[\s\-()]", "", values["phone"])
    mobile = _MOBILE.match(phone)
    if mobile:
        values["phone"] = mobile.group(1)
    else:
        errors["phone"] = "Enter a 10-digit mobile number."
    if len(values["line1"]) < 5:
        errors["line1"] = "Enter the house or flat number and street."
    if len(values["city"]) < 2:
        errors["city"] = "Enter a town or city."
    if values["state"] not in INDIAN_STATES:
        errors["state"] = "Choose a state."
    if not _PINCODE.match(values["pincode"]):
        errors["pincode"] = "Enter a 6-digit PIN code."

    # Generous limits that still keep a pasted essay out of the database
    for key, limit in (("name", 100), ("line1", 200), ("line2", 200), ("city", 100)):
        if len(values[key]) > limit:
            errors[key] = "That is too long."
    return values, errors


def one_line(address: dict[str, str]) -> str:
    """'Flat 4B, MG Road, Pune, Maharashtra 411001' for summaries."""
    parts = [address.get("line1"), address.get("line2"), address.get("city")]
    head = ", ".join(p for p in parts if p)
    return f"{head}, {address.get('state', '')} {address.get('pincode', '')}".strip(", ")


# --- addresses typed into a WhatsApp chat --------------------------------------

ASK_ADDRESS = (
    "Where should we deliver it? Send the address in one message, like this:\n\n"
    "Aisha Khan, 9876543210\n"
    "12 MG Road, Indiranagar\n"
    "Bengaluru 560038, Karnataka"
)

# What to ask for when something is missing, in the order we ask
_ASK_FOR = {
    "name": "What name should the parcel go to?",
    "phone": "What is the 10-digit mobile number for delivery?",
    "line1": "What is the house or flat number and street?",
    "city": "Which town or city?",
    "pincode": "What is the 6-digit PIN code?",
    "state": "Which state?",
}


def _state_in(text: str) -> str:
    """The Indian state named anywhere in the text (longest match wins)."""
    lowered = text.lower()
    found = [s for s in INDIAN_STATES if re.search(rf"\b{re.escape(s.lower())}\b", lowered)]
    return max(found, key=len) if found else ""


def parse_chat_address(text: str, known: dict[str, str] | None = None) -> dict[str, str]:
    """Pull an address out of a WhatsApp message, keeping anything already known.

    Customers write it as they would for a courier: a name and number, the
    street, then the town with its PIN, and the state, split over lines or
    commas. The phone, PIN and state are recognised for certain; the town is
    whatever sits beside the PIN (or the last piece), and the rest is the street.
    """
    values = {key: (known or {}).get(key, "") for key in FIELDS}
    chunks = [" ".join(c.split()) for c in re.split(r"[\n,;]+", str(text or ""))]
    chunks = [c for c in chunks if c]

    def take(pattern: str) -> tuple[str, int]:
        """Cut the first match out of the chunks; returns (match, chunk index)."""
        for i, chunk in enumerate(chunks):
            found = re.search(pattern, chunk)
            if found:
                chunks[i] = " ".join(chunk.replace(found.group(0), " ").split())
                return found.group(1 if found.groups() else 0), i
        return "", -1

    phone, _ = take(r"(?<!\d)(?:\+?91[\s-]?|0)?([6-9]\d{9})(?!\d)")
    pincode, pin_at = take(r"(?<!\d)([1-9]\d{5})(?!\d)")
    state = _state_in(" ".join(chunks))
    if state:
        chunks = [" ".join(re.sub(rf"\b{re.escape(state)}\b", " ", c, flags=re.I).split()) for c in chunks]
    values["phone"] = values["phone"] or phone
    values["pincode"] = values["pincode"] or pincode
    values["state"] = values["state"] or state

    city = chunks[pin_at] if 0 <= pin_at < len(chunks) else ""
    if city:
        chunks[pin_at] = ""
    chunks = [c for c in chunks if len(c) > 1]

    if chunks and not values["name"] and not re.search(r"\d", chunks[0]):
        values["name"] = chunks.pop(0)
    if not city and chunks:
        city = chunks.pop()                      # the last piece is the town
    values["city"] = values["city"] or city
    if chunks and not values["line1"]:
        values["line1"] = chunks.pop(0)
    if chunks and not values["line2"]:
        values["line2"] = " ".join(chunks)
    return values


def missing_fields(values: dict[str, str]) -> list[str]:
    """Which fields still need asking about, in the order to ask."""
    _, errors = validate(values)
    return [key for key in _ASK_FOR if key in errors]


def ask_for(field: str) -> str:
    return _ASK_FOR.get(field, "Could you send the address again?")
