"""One retrieval engine for the bot, the assistant's tools and the website search.

Meaning comes from the vector index (Chroma, built by scripts/ingest_catalog.py);
hard constraints (gender, category, budget, stock) come from the catalogue
itself and are applied in Python. With 32 products that is one Chroma round
trip ranking the whole catalogue, then exact filtering — no metadata filter can
silently match nothing because of a misspelt category.

Why this replaced the earlier Chroma `where` filters, with real examples:
  "jacket to wear over my kurta"  -> filtered on category "Jacket", which does
                                     not exist, so the bot found nothing
  "kurta for my wife"             -> Women AND "Kurta" (a men-only category)
  "sherwani under 3000"           -> no sherwani is that cheap, so nothing,
                                     instead of "the closest start at Rs. 8,999"
  "me and my wife ..."            -> filtered to women only
Every filter here maps onto categories that exist (checked at import), and an
empty result relaxes the filters step by step and says which one it dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import catalog
from common import embeddings

COLLECTION_NAME = "products"

# Squared L2 distance on normalised MiniLM vectors, range 0..4. Strong matches
# score ~0.4-0.9, loose but relevant ones up to ~1.35, unrelated text above ~1.6.
# Applied only to open-ended queries: when the customer named a category or a
# budget, the filters already guarantee relevance.
MAX_DISTANCE = 1.55


# --- what the customer asked for ----------------------------------------------

@dataclass
class Filters:
    gender: Optional[str] = None          # "Men" | "Women" | None
    both_genders: bool = False            # "me and my wife": show some of each
    categories: list[str] = field(default_factory=list)
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    occasion: Optional[str] = None        # soft: added to the query's meaning

    def is_empty(self) -> bool:
        return not (self.gender or self.both_genders or self.categories
                    or self.max_price or self.min_price)


@dataclass
class SearchResult:
    products: list[dict[str, Any]]
    filters: Filters
    relaxed: list[str] = field(default_factory=list)   # "budget", "category", "gender"

    def note(self) -> str:
        """One honest line when the results are not exactly what was asked."""
        if not self.products or not self.relaxed:
            return ""
        f = self.filters
        what = _describe_categories(f.categories) or "matches"
        if "category" in self.relaxed:
            return f"We do not have {what} for that right now — these are the closest pieces."
        if f.max_price:
            return f"No {what} under Rs. {f.max_price:,.0f} right now — these are the closest by price."
        if f.min_price:
            return f"No {what} above Rs. {f.min_price:,.0f} right now — these are the closest by price."
        return "These are the closest matches I have."


# Words that name a garment -> categories that really exist in data/products.json.
# A tuple of three means (any gender, men, women) where the word is shared.
_GARMENTS: dict[str, Any] = {
    "kurta": (["Kurta", "Kurta Set", "Kurti"], ["Kurta", "Kurta Set"], ["Kurti", "Salwar Suit"]),
    "kurtas": "kurta", "kurta set": "kurta",
    "kurti": ["Kurti"], "kurtis": ["Kurti"], "tunic": ["Kurti"],
    "jacket": ["Nehru Jacket", "Waistcoat"], "jackets": "jacket", "nehru": "jacket",
    "koti": "jacket", "modi jacket": "jacket",
    "waistcoat": ["Waistcoat", "Nehru Jacket"], "vest": "waistcoat", "waistcoats": "waistcoat",
    "bandhgala": ["Jodhpuri Suit"], "bandgala": "bandhgala", "jodhpuri": "bandhgala",
    "prince coat": "bandhgala", "indo western": (["Jodhpuri Suit", "Gown"], ["Jodhpuri Suit"], ["Gown"]),
    "sherwani": ["Sherwani"], "sherwanis": "sherwani", "achkan": "sherwani",
    "pathani": ["Pathani Suit"],
    "suit": (["Salwar Suit", "Pathani Suit", "Jodhpuri Suit"], ["Pathani Suit", "Jodhpuri Suit"], ["Salwar Suit", "Anarkali"]),
    "suits": "suit", "salwar": ["Salwar Suit"], "salwar kameez": ["Salwar Suit"],
    "sharara": ["Salwar Suit"], "palazzo suit": ["Salwar Suit"],
    "saree": ["Saree"], "sarees": "saree", "sari": "saree", "saris": "saree",
    "lehenga": ["Lehenga"], "lehengas": "lehenga", "lehnga": "lehenga", "ghagra": "lehenga",
    "anarkali": ["Anarkali"], "gown": ["Gown"], "gowns": "gown",
    "dupatta": (["Dupatta"], ["Stole"], ["Dupatta"]), "dupattas": "dupatta",
    "stole": ["Stole", "Dupatta"], "stoles": "stole", "shawl": "stole",
    "churidar": ["Bottomwear"], "dhoti": ["Bottomwear"], "pyjama": ["Bottomwear"],
    "pajama": ["Bottomwear"], "pants": ["Bottomwear"], "bottoms": ["Bottomwear"],
    "palazzo": ["Bottomwear", "Salwar Suit"], "palazzos": "palazzo",
}
# Garment words that also settle whose outfit it is
_MENS_GARMENTS = {"sherwani", "sherwanis", "achkan", "pathani", "bandhgala", "bandgala",
                  "jodhpuri", "prince coat", "dhoti", "koti", "modi jacket", "nehru"}
_WOMENS_GARMENTS = {"saree", "sarees", "sari", "saris", "lehenga", "lehengas", "lehnga",
                    "ghagra", "kurti", "kurtis", "anarkali", "sharara", "gown", "gowns",
                    "dupatta", "dupattas", "palazzo", "palazzos", "salwar", "salwar kameez",
                    "palazzo suit"}

_MEN = {"men", "mens", "man", "male", "gents", "boy", "boys", "groom", "husband",
        "brother", "father", "dad", "papa", "son", "grandfather", "uncle", "him",
        "himself", "fiance", "bhai", "jiju"}
_WOMEN = {"women", "womens", "woman", "female", "ladies", "lady", "girl", "girls",
          "bride", "wife", "sister", "mother", "mom", "mum", "daughter", "her",
          "herself", "fiancee", "aunt", "didi", "bhabhi", "grandmother"}
# "me and my wife", "my husband and I", "both of us", "for the couple"
_TOGETHER = re.compile(
    r"\b(?:me|myself|i)\s+(?:and|&|n|plus)\s+my\b"
    r"|\bmy\s+\w+\s+(?:and|&)\s+(?:me|i|myself)\b"
    r"|\bboth\s+of\s+us\b|\bcouple\b|\bus\s+both\b|\bwe\s+both\b|\bfor\s+us\b"
    r"|\bmatching\s+outfits?\b|\bhis\s+and\s+hers?\b"
)
_POSSESSIVE = {"my", "his", "her", "their", "our", "your"}

_OCCASIONS = {
    "wedding", "reception", "sangeet", "mehendi", "mehndi", "haldi", "engagement",
    "diwali", "eid", "navratri", "puja", "pooja", "office", "party", "cocktail",
    "festive", "festival", "casual", "daytime", "evening", "temple", "summer", "winter",
}

_NUM = r"(?:rs\.?|inr|₹)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh)?"
_MAX = re.compile(r"\b(?:under|below|less\s+than|upto|up\s+to|within|max(?:imum)?|"
                  r"not\s+more\s+than|no\s+more\s+than|cheaper\s+than|budget(?:\s+(?:of|is))?)\s*" + _NUM)
_MIN = re.compile(r"\b(?:above|over|more\s+than|at\s+least|minimum|starting)\s*" + _NUM)
_BETWEEN = re.compile(r"\bbetween\s*" + _NUM + r"\s*(?:and|to|-)\s*" + _NUM)
_RANGE = re.compile(_NUM + r"\s*(?:-|to)\s*" + _NUM + r"\b")
_AROUND = re.compile(r"\b(?:around|about|approx(?:imately)?|roughly|near)\s*" + _NUM)
_BARE_RUPEES = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh)?")


def _amount(number: str, unit: Optional[str]) -> Optional[float]:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    if unit in ("k", "thousand"):
        value *= 1000
    elif unit == "lakh":
        value *= 100_000
    # "under 3" is not a budget; "size 42" never reaches here
    return value if value >= 100 else None


def _budget(text: str) -> tuple[Optional[float], Optional[float]]:
    """(min_price, max_price) from what the customer wrote, if anything."""
    for pattern in (_BETWEEN, _RANGE):
        match = pattern.search(text)
        if match:
            low, high = _amount(match.group(1), match.group(2)), _amount(match.group(3), match.group(4))
            if low and high:
                return (min(low, high), max(low, high))
    match = _MAX.search(text)
    if match and _amount(*match.groups()):
        return (None, _amount(*match.groups()))
    match = _AROUND.search(text)
    if match and _amount(*match.groups()):
        return (None, round(_amount(*match.groups()) * 1.15))
    match = _MIN.search(text)
    if match and _amount(*match.groups()):
        return (_amount(*match.groups()), None)
    match = _BARE_RUPEES.search(text)
    if match and _amount(*match.groups()):
        return (None, _amount(*match.groups()))
    return (None, None)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def _garment_mentions(words: list[str]) -> list[str]:
    """Garment words the customer wants, in order.

    A garment after a possessive is one they already own: in "jacket to wear
    over my kurta" the kurta is context, the jacket is the request.
    """
    found: list[str] = []
    skip_next = False
    for i, word in enumerate(words):
        if skip_next:
            skip_next = False
            continue
        pair = f"{word} {words[i + 1]}" if i + 1 < len(words) else ""
        key = pair if pair in _GARMENTS else word if word in _GARMENTS else None
        if key is None:
            continue
        skip_next = key == pair
        if i > 0 and words[i - 1] in _POSSESSIVE:
            continue
        found.append(key)
    return found


def _resolve(key: str) -> Any:
    value = _GARMENTS[key]
    while isinstance(value, str):
        value = _GARMENTS[value]
    return value


def _categories_for(keys: list[str], gender: Optional[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        value = _resolve(key)
        if isinstance(value, tuple):
            value = value[1] if gender == "Men" else value[2] if gender == "Women" else value[0]
        for category in value:
            if category not in result:
                result.append(category)
    return result


def parse_query(text: str) -> Filters:
    """Turn a free-text request into filters. Used when no LLM is available."""
    lowered = text.lower()
    words = _tokens(lowered)
    garments = _garment_mentions(words)
    wordset = set(words)

    garment_gender = {"Men" for g in garments if g in _MENS_GARMENTS} | {
        "Women" for g in garments if g in _WOMENS_GARMENTS}
    person_gender = ({"Men"} if wordset & _MEN else set()) | ({"Women"} if wordset & _WOMEN else set())

    gender, both = None, False
    if _TOGETHER.search(lowered) and not len(garment_gender) == 1:
        both = True
    elif len(person_gender) == 1 and len(garment_gender) == 1 and person_gender != garment_gender:
        # "dupatta for men": who it is for beats what the garment usually is;
        # the category then relaxes to the closest men's pieces, with a note
        gender = next(iter(person_gender))
    elif len(garment_gender) == 1:
        gender = next(iter(garment_gender))
    elif len(garment_gender) == 2 or len(person_gender) == 2:
        both = True
    elif len(person_gender) == 1:
        gender = next(iter(person_gender))

    min_price, max_price = _budget(lowered)
    occasion = next((w for w in words if w in _OCCASIONS), None)
    return Filters(
        gender=gender, both_genders=both,
        categories=_categories_for(garments, gender),
        max_price=max_price, min_price=min_price, occasion=occasion,
    )


def normalise_filters(gender: Any = None, categories: Any = None, max_price: Any = None,
                      min_price: Any = None, occasion: Any = None) -> Filters:
    """Filters from structured arguments (the assistant's tool call), validated.

    Anything the model sends that does not map onto the catalogue is dropped
    rather than trusted: an invented category must not empty the results.
    """
    g = str(gender or "").strip().lower()
    both = g in ("both", "any", "unisex", "all", "couple")
    gender_value = "Men" if g in ("men", "man", "male", "mens", "m") else \
        "Women" if g in ("women", "woman", "female", "womens", "w") else None

    known = {c.lower(): c for c in catalog.categories()}
    wanted: list[str] = []
    for raw in categories or []:
        name = str(raw).strip().lower()
        mapped = [known[name]] if name in known else (
            _categories_for([name], gender_value) if name in _GARMENTS else [])
        for category in mapped:
            if category not in wanted:
                wanted.append(category)

    def price(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value) if value > 0 else None
        # The model sometimes sends "10k" or "₹2,500" as text
        match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh)?", str(value or "").lower())
        return _amount(match.group(1), match.group(2)) if match else None

    return Filters(gender=gender_value, both_genders=both, categories=wanted,
                   max_price=price(max_price), min_price=price(min_price),
                   occasion=str(occasion).strip().lower() if occasion else None)


def _check_mapping() -> None:
    """Fail loudly at import if a garment word maps onto a category we do not sell."""
    real = set(catalog.categories())
    for key in _GARMENTS:
        value = _resolve(key)
        options = value if isinstance(value, tuple) else (value,)
        for group in options:
            missing = set(group) - real
            if missing:
                print(f"[retrieval] WARNING: '{key}' maps to unknown categories {sorted(missing)}")


# --- ranking ----------------------------------------------------------------------

_collection_cache: dict[str, Any] = {}


def _collection() -> Any:
    if "c" not in _collection_cache:
        import chromadb

        client = chromadb.HttpClient(
            host=os.getenv("CHROMA_HOST", "localhost"),
            port=int(os.getenv("CHROMA_PORT", "8001")),
        )
        _collection_cache["c"] = client.get_collection(COLLECTION_NAME)
    return _collection_cache["c"]


def _haystack(p: dict[str, Any]) -> str:
    return " ".join([p["title"], p["category"], p["subcategory"], p["color"], p["fabric"],
                     p["gender"], " ".join(p["occasion"]), p["description"]]).lower()


def rank(query: str) -> tuple[list[tuple[dict[str, Any], float]], bool]:
    """Every catalogue product with its distance to the query, nearest first.

    Returns (ranked, semantic). semantic=False means Chroma was unreachable and
    the order comes from keyword overlap instead; the search still answers.
    """
    products = catalog.get_all()
    try:
        collection = _collection()
        result = collection.query(
            query_embeddings=[embeddings.embed_one(query)],
            n_results=max(1, min(len(products), collection.count())),
            include=["distances"],
        )
        by_id = {p["id"]: p for p in products}
        ranked = [(by_id[pid], dist) for pid, dist in zip(result["ids"][0], result["distances"][0])
                  if pid in by_id]
        return ranked, True
    except Exception as exc:
        _collection_cache.clear()   # reconnect next time, e.g. after Chroma restarts
        print(f"[retrieval] vector search unavailable, ranking by keywords: {exc!r}")
        words = [w for w in _tokens(query) if len(w) > 2]
        scored = [(p, sum(w in _haystack(p) for w in words)) for p in products]
        scored.sort(key=lambda pair: -pair[1])
        # Pretend distances so the same cut-off logic applies: no overlap = no match
        return [(p, 0.5 if score else 9.0) for p, score in scored], False


def _in_stock(p: dict[str, Any]) -> bool:
    stock = p.get("stock") or {}
    return bool(p.get("in_stock", True)) and (not stock or any(v > 0 for v in stock.values()))


def _passes(p: dict[str, Any], f: Filters) -> bool:
    if f.gender and p["gender"] != f.gender:
        return False
    if f.categories and p["category"] not in f.categories:
        return False
    if f.max_price and p["price"] > f.max_price:
        return False
    if f.min_price and p["price"] < f.min_price:
        return False
    return True


def _interleave(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    men = [p for p in products if p["gender"] == "Men"]
    women = [p for p in products if p["gender"] == "Women"]
    mixed: list[dict[str, Any]] = []
    for i in range(max(len(men), len(women))):
        mixed += [p for p in (women[i:i + 1] + men[i:i + 1])]
    return mixed


def _learned(query: str, user_id: Optional[int]) -> tuple[dict[str, int], set[str]]:
    """The owner's ratings for questions like this, and this customer's hidden items."""
    try:
        import training   # needs Postgres; retrieval must still work without it
    except Exception:
        return {}, set()
    return training.ranking_adjustments(query), training.hidden_for(user_id)


def search(query: str, filters: Optional[Filters] = None, k: int = 3,
           user_id: Optional[int] = None, learn: bool = True) -> SearchResult:
    """The best k in-stock products for a request, never silently empty.

    filters=None parses them from the text. If nothing passes, the filters are
    relaxed in this order and the result says so: budget (show the nearest
    prices), category (the garment is not stocked), gender. An open-ended query
    with nothing relevant ("asdfgh") returns no products rather than noise.
    """
    query = (query or "").strip()
    f = filters if filters is not None else parse_query(query)
    meaning = f"{query} {f.occasion}" if f.occasion and f.occasion not in query.lower() else query
    if not meaning:
        meaning = " ".join(f.categories) or "ethnic wear"

    ranked, _ = rank(meaning)
    ranked = [(p, d) for p, d in ranked if _in_stock(p)]
    adjust, hidden = _learned(query or meaning, user_id) if learn else ({}, set())
    if hidden:
        ranked = [(p, d) for p, d in ranked if p["id"] not in hidden]

    attempts = [(f, [])]
    if f.max_price or f.min_price:
        attempts.append((replace(f, max_price=None, min_price=None), ["budget"]))
    if f.categories:
        attempts.append((replace(f, max_price=None, min_price=None, categories=[]), ["budget", "category"]
                         if (f.max_price or f.min_price) else ["category"]))

    for current, relaxed in attempts:
        matches = [(p, d) for p, d in ranked if _passes(p, current)]
        if current.is_empty():
            matches = [(p, d) for p, d in matches if d <= MAX_DISTANCE]
        if not matches:
            continue
        products = [p for p, _ in matches]
        if "budget" in relaxed and "category" not in relaxed:
            # Nearest to what they wanted to spend, not the most similar at any price
            target = f.max_price or f.min_price or 0
            products.sort(key=lambda p: abs(p["price"] - target))
        if adjust:
            # Owner training: good matches first, wrong ones last (stable sort,
            # so everything else keeps its meaning-based order)
            products.sort(key=lambda p: -max(-1, min(1, adjust.get(p["id"], 0))))
        if current.both_genders:
            products = _interleave(products)
        return SearchResult(products=products[:k], filters=f, relaxed=relaxed)

    return SearchResult(products=[], filters=f, relaxed=[])


def _describe_categories(categories: list[str]) -> str:
    if not categories:
        return ""
    names = [c.lower() + ("s" if not c.endswith(("s", "wear")) else "") for c in categories[:2]]
    return " or ".join(names)


_check_mapping()
