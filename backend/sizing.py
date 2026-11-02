"""Size guide: which size fits, decided by code from the customer's own numbers.

"What size am I?" is a fact like a price, so the model never answers it. It
calls get_size_guide (or the webhook answers directly when an item is picked),
and this module reads the measurements from the customer's words ("chest 41",
"my waist is 32 inches", "foot 26.5 cm"), picks the size from a chart, and
writes the chart and the answer. The model's own sentence around it still
goes through the size check, so it cannot name a size the item lacks.

Charts are body measurements, by the kind of sizes a product comes in:
  letters (S, M, L ...)   chest/bust and waist in inches, by gender
  numbers (38, 40 ...)    chest in inches: a 40 fits a 39-40 chest
                          (waist for bottoms: a 32 fits a 31-32 waist)
  UK n                    foot length in cm, by gender
  Free Size               nothing to choose
A brand may replace any chart in its settings (stores.brand "size_chart"):
  {"<Category>" or "*": {"chest": {"S": [35, 36], ...}, "waist": {...}}}

Between two sizes, the larger is chosen: a garment a little loose can be
altered, one too tight cannot.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import brand

# Body measurements in inches: (low, high) per size
_MEN = {
    "chest": {"XS": (33, 34), "S": (35, 36), "M": (37, 38), "L": (39, 40), "XL": (41, 42),
              "XXL": (43, 44), "XXXL": (45, 46)},
    "waist": {"XS": (27, 28), "S": (29, 30), "M": (31, 32), "L": (33, 34), "XL": (35, 36),
              "XXL": (37, 38), "XXXL": (39, 40)},
}
_WOMEN = {
    "chest": {"XS": (31, 32), "S": (33, 34), "M": (35, 36), "L": (37, 38), "XL": (39, 40),
              "XXL": (41, 42), "XXXL": (43, 44)},
    "waist": {"XS": (25, 26), "S": (27, 28), "M": (29, 30), "L": (31, 32), "XL": (33, 34),
              "XXL": (35, 36), "XXXL": (37, 38)},
}
# Foot length in cm, heel to longest toe: up to this length
_FEET = {
    "Men": {"5": 23.7, "6": 24.5, "7": 25.4, "8": 26.2, "9": 27.1, "10": 27.9, "11": 28.8, "12": 29.6},
    "Women": {"2": 21.2, "3": 22.0, "4": 22.9, "5": 23.7, "6": 24.6, "7": 25.4, "8": 26.2, "9": 27.1},
}
_LETTERS = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]

# Bottoms are sized by the waist, tops by the chest, sets and dresses by both
_BOTTOMS = re.compile(r"bottom|trouser|pant|jean|jogger|short|legging|skirt|churidar|palazzo|dhoti$", re.I)
_TOPS_ONLY = re.compile(r"kurta$|kurti|shirt|tee|t-shirt|top|jacket|waistcoat|blazer|hoodie|sweat|bra|tank|vest",
                        re.I)

# --- reading the customer's numbers ----------------------------------------------

_NUM = r"(\d{2,3}(?:\.\d)?)"
_UNIT = r"\s*(cm|cms|centimet(?:er|re)s?|in|inch|inches|\"|”)?"


def _inches(value: str, unit: Optional[str]) -> float:
    number = float(value)
    # "chest 102 cm", or "chest 102" with no unit: nobody has a 102-inch chest
    if (unit or "").startswith("c") or number > 60:
        number /= 2.54
    return round(number, 1)


def _last(patterns: list[str], said: str) -> Optional[re.Match]:
    """The latest match of any pattern: text is several messages, oldest first,
    and "bust 35" for a lehenga earlier must not size today's "chest 39" kurta."""
    matches = [m for pattern in patterns for m in re.finditer(pattern, said)]
    return max(matches, key=lambda m: m.start()) if matches else None


def measurements(text: str) -> dict[str, float]:
    """Body measurements the customer typed: chest and waist in inches, foot in cm."""
    said = text.lower()
    found: dict[str, float] = {}
    for key, words in (("chest", r"chest|bust"), ("waist", r"waist")):
        match = _last([rf"(?:{words})(?:\s+(?:size|is|of|measures?|around|about|=|:))*\s*{_NUM}{_UNIT}",
                       rf"{_NUM}{_UNIT}\s*(?:{words})"], said)
        if match:
            found[key] = _inches(match.group(1), match.group(2))
    foot = _last([r"(?:foot|feet)(?:\s+(?:length|size|is|measures?|about|=|:))*\s*(\d{2}(?:\.\d)?)\s*(?:cm|cms)",
                  r"(\d{2}(?:\.\d)?)\s*(?:cm|cms)\s*(?:long\s+)?(?:foot|feet)"], said)
    if foot:
        found["foot_cm"] = float(foot.group(1))
    return found


_ASKS = re.compile(
    r"\b(?:what|which)\s+size\b|\bsize\s+(?:guide|chart)\b|\bmy\s+size\b|\bmeasure|"
    r"\b(?:will|would|does)\s+(?:it|this|that|they|these|size\s+\w+|an?\s+\w+|the\s+\w+|\w{1,4})\s+fit\b|"
    r"\bhow\s+(?:does|do)\b.{0,20}\bfit\b", re.I)


def asks_for_size_help(text: str) -> bool:
    """A sizing question or a body measurement, not a size being chosen:
    "my chest is 40, which size?" must not add a 40 to the cart."""
    return bool(_ASKS.search(text) or measurements(text))


# --- charts ------------------------------------------------------------------------

def _uk(size: str) -> Optional[str]:
    match = re.fullmatch(r"uk\s*(\d{1,2})", size.strip().lower())
    return match.group(1) if match else None


def _kind(sizes: list[str]) -> str:
    if not sizes or all(s.lower() in ("free size", "one size") for s in sizes):
        return "free"
    if all(_uk(s) for s in sizes):
        return "feet"
    if all(re.fullmatch(r"\d{2}", s) for s in sizes):
        return "numbers"
    if all(s.upper() in _LETTERS for s in sizes):
        return "letters"
    return "other"


def _measures(product: dict[str, Any]) -> list[str]:
    category = str(product.get("category") or "")
    if _BOTTOMS.search(category):
        return ["waist"]
    if _TOPS_ONLY.search(category):
        return ["chest"]
    if product.get("gender") == "Women":
        return ["chest", "waist"]
    return ["chest"]


def chart(product: dict[str, Any]) -> dict[str, dict[str, tuple[float, float]]]:
    """{measure: {size: (low, high)}} for this product's own sizes."""
    sizes = list(product.get("sizes_available") or [])
    kind = _kind(sizes)
    override = _override(product)
    if override:
        return {m: {s: tuple(r) for s, r in rows.items() if s in sizes} for m, rows in override.items()}
    if kind == "letters":
        table = _WOMEN if product.get("gender") == "Women" else _MEN
        return {m: {s: table[m][s.upper()] for s in sizes if s.upper() in table[m]} for m in _measures(product)}
    if kind == "numbers":
        measure = "waist" if _BOTTOMS.search(str(product.get("category") or "")) else "chest"
        return {measure: {s: (int(s) - 1, int(s)) for s in sizes}}
    if kind == "feet":
        table = _FEET["Women" if product.get("gender") == "Women" else "Men"]
        rows: dict[str, tuple[float, float]] = {}
        previous = None
        for size in sorted(sizes, key=lambda s: int(_uk(s) or 0)):
            longest = table.get(_uk(size) or "")
            if longest is None:
                continue
            rows[size] = (previous + 0.1 if previous else longest - 0.8, longest)
            previous = longest
        return {"foot_cm": rows}
    return {}


def _override(product: dict[str, Any]) -> dict[str, dict[str, list[float]]]:
    try:
        charts = brand.current().size_chart
    except Exception:
        return {}
    return charts.get(str(product.get("category") or "")) or charts.get("*") or {}


# --- the answer --------------------------------------------------------------------

_LABEL = {"chest": "chest", "waist": "waist", "foot_cm": "foot length"}
_UNITS = {"chest": "in", "waist": "in", "foot_cm": "cm"}


def _label(measure: str, product: dict[str, Any]) -> str:
    return "bust" if measure == "chest" and product.get("gender") == "Women" else _LABEL[measure]


def recommend(product: dict[str, Any], said: dict[str, float]) -> tuple[Optional[str], str, bool]:
    """(size, why, fits) from the chart and the measurements given; fits is
    False when they are beyond our largest size. (None, "", False) if nothing
    they gave applies to this product."""
    table = chart(product)
    order = list(product.get("sizes_available") or [])
    picks: list[tuple[str, str]] = []
    fits = True
    for measure, rows in table.items():
        if measure not in said or not rows:
            continue
        value = said[measure]
        fitting = [s for s, (_, high) in rows.items() if value <= high + 0.05]
        size = fitting[0] if fitting else list(rows)[-1]
        fits = fits and bool(fitting)
        picks.append((size, f"{_label(measure, product)} {value:g} {_UNITS[measure]} → {size}"))
    if not picks:
        return None, "", False
    # Chest says M, waist says L: the larger, or the set will not close
    size = max((s for s, _ in picks), key=lambda s: order.index(s) if s in order else -1)
    return size, "; ".join(why for _, why in picks), fits


def _range(low: float, high: float, unit: str) -> str:
    if unit == "cm":
        return f"up to {high:g}"
    return f"{low:g}–{high:g}" if low != high else f"{high:g}"


def guide(product: dict[str, Any], text: str, in_stock: Optional[list[str]] = None) -> str:
    """The size guide for one product, as sent on WhatsApp: the chart for its
    sizes and, when the customer gave a measurement, the size that fits."""
    name = product.get("name") or product.get("id")
    sizes = list(product.get("sizes_available") or [])
    in_stock = sizes if in_stock is None else in_stock
    if _kind(sizes) == "free":
        return f"*{name}* is one size ({sizes[0] if sizes else 'Free Size'}): nothing to choose. Reply ADD to add it."
    table = chart(product)
    if not table or not any(table.values()):
        return (f"*{name}* comes in {', '.join(sizes)}. I do not have a size chart for it yet: "
                "tell me the size you usually wear in this kind of piece.")
    lines = [f"*Size guide · {name}*"]
    for measure, rows in table.items():
        unit = _UNITS[measure]
        heading = "Foot length (cm)" if measure == "foot_cm" else f"Body {_label(measure, product)} (inches)"
        lines.append(f"{heading}: " + " · ".join(f"{s}: {_range(lo, hi, unit)}" for s, (lo, hi) in rows.items()))
    said = measurements(text)
    size, why, fits = recommend(product, said)
    if size:
        lines.append("")
        if not fits:
            lines.append(f"Our largest is *{size}*, which would be too tight for you, so I would not "
                         "suggest this piece. Tell me if you would like me to look for something with more room.")
        elif size in in_stock:
            lines.append(f"Your {why}. *{size}* is your size, and it is in stock.")
            lines.append(f"Reply ADD {size} to add it to your cart.")
        else:
            lines.append(f"Your {why}. *{size}* is your size, but it is sold out right now.")
        return "\n".join(lines)
    wanted = [m for m in table if m != "foot_cm"]
    if "foot_cm" in table:
        lines.append("Measure your foot from heel to longest toe and send it in cm (for example \"foot 26 cm\"):"
                     " I will pick your size.")
    else:
        names = [_label(m, product) for m in wanted]
        example = " ".join(f"{n} {34 if n == 'bust' else 38 if n == 'chest' else 30}" for n in names)
        lines.append(f"Send your {' and '.join(names)} in inches (for example \"{example}\")"
                     " and I will pick your size.")
    return "\n".join(lines)
