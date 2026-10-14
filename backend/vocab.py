"""The words customers use, mapped onto each store's own catalogue.

retrieval.parse_query turns "black tee under 1000 for my brother" into
filters. Which words name a category, which settle whose outfit it is, and
which name an occasion depend on what the store sells, so they are built per
store:

  * from the catalogue: every category's name in its usual spellings ("T-Shirts"
    -> "t-shirts", "t shirts", "tshirts", "t shirt", "tee"...), a gender when all of
    a category's products are for one, and occasion words from the products'
    occasion tags and Shopify tags ("gym", "yoga", "running")
  * from a vertical pack, when the store's brand settings name one: hand-written
    vocabulary for a kind of shop (PACKS; "indian_ethnic" is the demo's: "kurta for
    my wife" -> kurtis, "jodhpuri" -> bandhgala, haldi/mehendi/sangeet). A pack
    replaces what the catalogue would give, so a store's search does not change
    when this module learns new spellings. Only categories the store sells are
    kept; a pack word for another category is dropped, never an empty filter.

Cached per store until the catalogue reloads (catalog.loaded_at).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import catalog
import tenancy


@dataclass
class Vocabulary:
    # word -> [categories], or (any, men, women) when the word is shared, or an alias (str)
    garments: dict[str, Any] = field(default_factory=dict)
    mens: set[str] = field(default_factory=set)        # garment words that settle "for a man"
    womens: set[str] = field(default_factory=set)
    occasions: set[str] = field(default_factory=set)
    occasion_tags: dict[str, set[str]] = field(default_factory=dict)   # word -> tags products carry
    accessories: set[str] = field(default_factory=set)  # categories shown only when asked for
    meaning: str = "clothing"                            # what an empty query means


# --- packs ------------------------------------------------------------------------------

INDIAN_ETHNIC: dict[str, Any] = {
    # Words that name a garment -> categories that really exist in data/products.json.
    # A tuple of three means (any gender, men, women) where the word is shared.
    "garments": {
        "kurta": (["Kurta", "Kurta Set", "Kurti"], ["Kurta", "Kurta Set"], ["Kurti", "Salwar Suit", "Co-ord Set"]),
        "kurtas": "kurta",
        "kurti": ["Kurti"], "kurtis": ["Kurti"], "tunic": ["Kurti"],
        "jacket": ["Nehru Jacket", "Waistcoat"], "jackets": "jacket", "nehru": "jacket",
        "koti": "jacket", "modi jacket": "jacket",
        "waistcoat": ["Waistcoat", "Nehru Jacket"], "vest": "waistcoat", "waistcoats": "waistcoat",
        "bandhgala": ["Jodhpuri Suit"], "bandgala": "bandhgala", "jodhpuri": "bandhgala",
        "prince coat": "bandhgala",
        "indo western": (["Indo-Western", "Jodhpuri Suit", "Gown"], ["Indo-Western", "Jodhpuri Suit"],
                         ["Indo-Western", "Gown"]),
        "indowestern": "indo western", "fusion": "indo western",
        "sherwani": ["Sherwani"], "sherwanis": "sherwani", "achkan": "sherwani",
        "pathani": ["Pathani Suit"],
        "suit": (["Salwar Suit", "Pathani Suit", "Jodhpuri Suit"], ["Pathani Suit", "Jodhpuri Suit"],
                 ["Salwar Suit", "Anarkali"]),
        "suits": "suit", "salwar": ["Salwar Suit"], "salwar kameez": ["Salwar Suit"],
        "sharara": ["Sharara & Gharara", "Salwar Suit"], "shararas": "sharara", "gharara": "sharara",
        "ghararas": "sharara", "palazzo suit": ["Salwar Suit"],
        "co ord": ["Co-ord Set"], "coord": "co ord", "co ords": "co ord", "coords": "co ord",
        "saree": ["Saree"], "sarees": "saree", "sari": "saree", "saris": "saree",
        "lehenga": ["Lehenga"], "lehengas": "lehenga", "lehnga": "lehenga", "ghagra": "lehenga",
        "chaniya": "lehenga", "chaniya choli": "lehenga", "choli": "lehenga", "chaniyacholi": "lehenga",
        "anarkali": ["Anarkali"], "gown": ["Gown"], "gowns": "gown",
        "dupatta": (["Dupatta"], ["Stole"], ["Dupatta"]), "dupattas": "dupatta", "phulkari": "dupatta",
        "stole": ["Stole", "Dupatta"], "stoles": "stole", "shawl": "stole",
        "churidar": ["Bottomwear"], "pyjama": ["Bottomwear"],
        "pajama": ["Bottomwear"], "pants": ["Bottomwear"], "bottoms": ["Bottomwear"],
        "palazzo": ["Bottomwear", "Salwar Suit"], "palazzos": "palazzo",
        "kurta pyjama": ["Kurta Set"], "kurta pajama": "kurta pyjama",
        "kurta set": (["Kurta Set", "Salwar Suit"], ["Kurta Set"], ["Salwar Suit", "Co-ord Set"]),
        "dhoti": ["Dhoti Kurta Set", "Bottomwear"], "dhoti kurta": ["Dhoti Kurta Set"],
        "veshti": "dhoti", "dhotis": "dhoti",
        # Accessories
        "safa": ["Headwear"], "saafa": "safa", "turban": "safa", "turbans": "safa", "pagdi": "safa",
        "pagri": "safa", "pag": "safa", "headwear": "safa",
        "shoes": ["Footwear"], "shoe": "shoes", "footwear": "shoes", "sandals": "shoes",
        "sandal": "shoes", "chappal": "shoes", "chappals": "shoes", "slippers": "shoes",
        "flats": "shoes", "heels": "shoes", "heel": "shoes", "jutti": "shoes", "juttis": "shoes",
        "juti": "shoes", "jootis": "shoes", "mojari": "shoes", "mojaris": "shoes", "mojri": "shoes",
        "nagra": "shoes", "kolhapuri": "shoes", "kolhapuris": "shoes", "footware": "shoes",
        "bag": ["Bags"], "bags": "bag", "potli": "bag", "potlis": "bag", "clutch": "bag",
        "clutches": "bag", "purse": "bag", "purses": "bag", "handbag": "bag", "sling": "bag",
        "accessories": (["Footwear", "Bags", "Headwear", "Dupatta", "Stole"],
                        ["Footwear", "Headwear", "Stole"], ["Footwear", "Bags", "Dupatta"]),
        "accessory": "accessories",
    },
    # Garment words that also settle whose outfit it is
    "mens": {"sherwani", "sherwanis", "achkan", "pathani", "bandhgala", "bandgala",
             "jodhpuri", "prince coat", "dhoti", "koti", "modi jacket", "nehru",
             "kurta pyjama", "kurta pajama", "dhoti kurta", "veshti", "dhotis", "safa", "saafa",
             "turban", "turbans", "pagdi", "pagri", "pag", "mojari", "mojaris", "mojri", "nagra"},
    "womens": {"saree", "sarees", "sari", "saris", "lehenga", "lehengas", "lehnga",
               "ghagra", "kurti", "kurtis", "anarkali", "sharara", "gown", "gowns",
               "dupatta", "dupattas", "palazzo", "palazzos", "salwar", "salwar kameez",
               "palazzo suit", "shararas", "gharara", "ghararas", "chaniya", "chaniya choli",
               "choli", "chaniyacholi", "phulkari", "heels", "heel", "jutti", "juttis", "juti",
               "jootis", "potli", "potlis", "clutch", "clutches", "purse", "purses", "handbag"},
    "occasions": {
        "wedding", "reception", "sangeet", "mehendi", "mehndi", "haldi", "engagement",
        "diwali", "eid", "navratri", "puja", "pooja", "office", "party", "cocktail",
        "festive", "festival", "casual", "daytime", "evening", "temple", "summer", "winter",
        "garba", "dandiya", "onam", "vishu", "lohri", "baraat", "bridal", "ceremony", "pheras",
    },
    # Occasion word -> the tags products carry for it (products tagged first, see search)
    "occasion_tags": {
        "mehndi": {"mehendi"}, "pooja": {"puja"}, "festival": {"festive"},
        "garba": {"garba", "navratri"}, "dandiya": {"garba", "navratri"}, "navratri": {"navratri", "garba"},
        "ceremony": {"wedding", "bridal"}, "pheras": {"wedding", "bridal"}, "bridal": {"bridal"},
        "baraat": {"baraat", "groom"}, "summer": {"summer wedding"}, "winter": {"winter wedding"},
        "party": {"party", "cocktail"}, "cocktail": {"cocktail", "party"},
    },
    # Shown only when asked for: "haldi outfit" should not return chappals
    "accessories": {"Footwear", "Bags", "Headwear"},
    "meaning": "ethnic wear",
}

PACKS: dict[str, dict[str, Any]] = {"indian_ethnic": INDIAN_ETHNIC}

# Everyday names for common apparel categories: any store whose category name
# contains the key also answers to these words ("Graphic Tees" -> "tee", "tees")
_SYNONYMS = {
    "t shirt": ["tee", "tees", "tshirt", "tshirts", "t shirts"],
    "tee": ["t shirt", "t shirts", "tshirt", "tshirts"],
    "sweatshirt": ["sweatshirts", "sweat shirt"], "hoodie": ["hoodies", "hoody"],
    "jogger": ["joggers", "track pants", "trackpants"], "short": ["shorts"],
    "sports bra": ["sports bras", "bra", "bras"], "legging": ["leggings", "tights"],
    "tank": ["tank top", "tank tops", "tanks", "vest", "vests", "sleeveless"],
    "jacket": ["jackets"], "trouser": ["trousers", "pants"], "cap": ["caps", "hat", "hats"],
    "sock": ["socks"], "shoe": ["shoes", "sneakers", "trainers", "footwear"], "bag": ["bags", "tote", "totes"],
}
# Categories that are add-ons, shown only when asked for by name
_ADD_ON = re.compile(r"accessor|\bbags?\b|footwear|shoe|\bcaps?\b|\bhats?\b|socks?|bottle|jewel|headwear|tote",
                     re.IGNORECASE)
# Tag words that never mean an occasion or activity
_NOT_OCCASIONS = {"men", "mens", "women", "womens", "unisex", "male", "female", "new", "sale", "default",
                  "all", "bestseller", "featured"}


# --- building ---------------------------------------------------------------------------

def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _spellings(category: str) -> set[str]:
    """"T-Shirts" -> t shirts, t shirt, tshirts, tshirt (+ synonyms such as tee)."""
    base = _plain(category)
    forms = {base}
    if base.endswith("s") and len(base) > 3:
        forms.add(base[:-1])
    else:
        forms.add(base + "s")
    forms |= {f.replace(" ", "") for f in forms if " " in f and len(f.split()[0]) <= 2}   # "t shirt" -> "tshirt"
    for key, extra in _SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(key)}", f) for f in forms):
            forms |= set(extra)
    return {f for f in forms if f}


def _from_catalogue(products: list[dict[str, Any]]) -> Vocabulary:
    v = Vocabulary()
    genders: dict[str, set[str]] = {}
    for p in products:
        if p.get("category"):
            genders.setdefault(p["category"], set()).add(p.get("gender") or "")
    for category, seen in sorted(genders.items()):
        for word in _spellings(category):
            current = v.garments.get(word)
            v.garments[word] = sorted(set(current or []) | {category}) if isinstance(current, list) else [category]
            if seen == {"Men"}:
                v.mens.add(word)
            elif seen == {"Women"}:
                v.womens.add(word)
        if _ADD_ON.search(category):
            v.accessories.add(category)
    # The noun a title ends with names what it is, inside broad categories:
    # "Gym Duffel Bag", "Training Cap" in "Accessories" (a customer says "gym bag")
    nouns: dict[str, set[str]] = {}
    noun_genders: dict[str, set[str]] = {}
    for p in products:
        title = re.sub(r"\(.*?\)|\s+-\s+.*$", " ", str(p.get("title") or p.get("name") or ""))
        words = re.findall(r"[a-z]+", title.lower())
        if not words or not p.get("category"):
            continue
        for word in _spellings(words[-1]):
            if word not in v.garments:
                nouns.setdefault(word, set()).add(p["category"])
                noun_genders.setdefault(word, set()).add(p.get("gender") or "")
    for word, cats in nouns.items():
        v.garments[word] = sorted(cats)
        if noun_genders[word] == {"Men"}:
            v.mens.add(word)
        elif noun_genders[word] == {"Women"}:
            v.womens.add(word)
    # A word shared by a men's and a women's category says nothing about gender
    v.mens, v.womens = v.mens - v.womens, v.womens - v.mens
    category_words = set(v.garments)
    for p in products:
        for tag in list(p.get("occasion") or []) + list(p.get("tags") or []):
            word = _plain(str(tag))
            if word and " " not in word and word not in _NOT_OCCASIONS and word not in category_words \
                    and not word.isdigit():
                v.occasions.add(word)
    v.meaning = " ".join(sorted(genders)[:3]).lower() or "clothing"
    return v


def _from_pack(pack: dict[str, Any], real: set[str]) -> Vocabulary:
    """The pack, keeping only categories this store sells."""
    def keep(value: Any) -> Any:
        if isinstance(value, str):
            return value
        if isinstance(value, tuple):
            groups = tuple([c for c in group if c in real] for group in value)
            return groups if any(groups) else None
        kept = [c for c in value if c in real]
        return kept or None

    garments = {word: kept for word, value in pack["garments"].items() if (kept := keep(value)) is not None}
    # Drop aliases whose target was dropped
    garments = {w: v for w, v in garments.items() if not isinstance(v, str) or v in garments}
    return Vocabulary(garments=garments, mens=set(pack["mens"]) & set(garments),
                      womens=set(pack["womens"]) & set(garments), occasions=set(pack["occasions"]),
                      occasion_tags={k: set(v) for k, v in pack["occasion_tags"].items()},
                      accessories=set(pack["accessories"]) & real, meaning=pack.get("meaning") or "clothing")


def build(products: list[dict[str, Any]], pack_name: Optional[str] = None) -> Vocabulary:
    derived = _from_catalogue(products)
    pack = PACKS.get(pack_name or "")
    if pack is None:
        if pack_name:
            print(f"[vocab] unknown vertical pack {pack_name!r}; using the catalogue's own words")
        return derived
    vocab = _from_pack(pack, {p["category"] for p in products if p.get("category")})
    # Categories the pack never mentions still get their own name
    reached = {c for value in vocab.garments.values() if not isinstance(value, str)
               for group in (value if isinstance(value, tuple) else (value,)) for c in group}
    for word, value in derived.garments.items():
        missing = [c for c in value if c not in reached]
        if missing and word not in vocab.garments:
            vocab.garments[word] = missing
            if word in derived.mens:
                vocab.mens.add(word)
            if word in derived.womens:
                vocab.womens.add(word)
    return vocab


_cache: dict[int, tuple[Any, Vocabulary]] = {}


def current() -> Vocabulary:
    """The current store's vocabulary, rebuilt when its catalogue reloads."""
    import brand

    sid = tenancy.store_id()
    stamp = (catalog.loaded_at(), brand.current().vertical_pack)
    hit = _cache.get(sid)
    if hit and hit[0] == stamp:
        return hit[1]
    vocab = build(catalog.get_all(), stamp[1])
    _cache[sid] = (stamp, vocab)
    return vocab
