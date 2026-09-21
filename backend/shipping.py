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
