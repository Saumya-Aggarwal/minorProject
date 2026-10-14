"""Colour, fabric and fit: what a customer asks for, and what each product is.

Enforced like gender and budget (P2 step 4): "black suit for my wedding"
shows only black suits, or says honestly that there are none and shows the
closest. Before this, colour was only part of the embedding's meaning: the
14 Oct eval asked for a black suit and got a white one, and a "yellow kurta"
in Hinglish got tangerine, lime green and olive.

Colours are matched by family, since nobody's "yellow" is a catalogue's
"Sunflower yellow" or "Mustard" word for word, and some colours belong to two
families (teal is blue and green; peach is pink and orange). A product's
families come from its colour, else its name; a request's from its words,
English or Hinglish ("pila", "kala"). Fabric and fit work the same way, read
from a product's fabric/fit fields, its Shopify tags and its name.

A colour that describes something else is not the request: "a dupatta to go
with my red lehenga", "anything but black".
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

# word -> colour families
COLOURS: dict[str, set[str]] = {}


def _add(families: str, *words: str) -> None:
    for word in words:
        COLOURS.setdefault(word, set()).update(families.split())


_add("black", "black", "kala", "kaala")
_add("white", "white", "safed")
_add("cream", "cream", "ivory", "beige", "sand", "ecru", "nude", "offwhite", "oatmeal")
_add("white cream", "off")             # "off white"
_add("grey", "grey", "gray", "charcoal", "slate", "melange", "graphite")
_add("blue", "blue", "navy", "indigo", "cobalt", "neela", "nila", "azure")
_add("blue green", "teal", "peacock", "aqua", "turquoise", "cyan")
_add("green", "green", "emerald", "sage", "olive", "lime", "mint", "hara", "khaki", "pista")
_add("yellow", "yellow", "mustard", "marigold", "canary", "lemon", "sunflower", "pila", "peela")
_add("orange", "orange", "tangerine", "saffron", "kesari", "amber", "apricot")
_add("orange brown", "rust")
_add("orange pink", "peach")
_add("red orange pink", "coral")
_add("red", "red", "crimson", "maroon", "cherry", "scarlet", "lal", "laal")
_add("red purple", "wine", "burgundy")
_add("pink", "pink", "blush", "rose", "rani", "fuchsia", "gulabi", "salmon")
_add("pink purple", "magenta")
_add("purple", "purple", "lilac", "plum", "lavender", "violet", "mauve", "jamuni")
_add("brown", "brown", "chestnut", "coffee", "chocolate", "camel", "mocha")
_add("brown cream", "tan")
_add("brown gold", "bronze")
_add("gold", "gold", "golden")
_add("gold cream", "champagne")
_add("silver", "silver")
_add("multi", "multicolour", "multicolor", "multicolored", "multicoloured")

# word -> fabric family
FABRICS: dict[str, str] = {
    "cotton": "cotton", "khadi": "cotton", "mul": "cotton", "voile": "cotton", "cambric": "cotton",
    "linen": "linen", "silk": "silk", "kanjivaram": "silk", "banarasi": "silk", "tussar": "silk",
    "velvet": "velvet", "georgette": "georgette", "chiffon": "chiffon", "satin": "satin", "net": "net",
    "organza": "organza", "rayon": "rayon", "viscose": "rayon", "modal": "modal", "polyester": "polyester",
    "poly": "polyester", "nylon": "nylon", "wool": "wool", "woollen": "wool", "woolen": "wool",
    "fleece": "fleece", "denim": "denim", "leather": "leather", "crepe": "crepe", "brocade": "brocade",
    "chanderi": "chanderi", "jersey": "jersey", "terry": "terry", "mesh": "mesh", "lycra": "stretch",
    "spandex": "stretch", "stretch": "stretch",
}
# word -> fit family
FITS: dict[str, str] = {
    "oversized": "oversized", "oversize": "oversized", "baggy": "oversized", "boxy": "oversized",
    "slim": "slim", "skinny": "slim", "fitted": "slim", "tapered": "tapered",
    "relaxed": "relaxed", "loose": "relaxed",
    "flared": "flared", "cropped": "cropped", "crop": "cropped",
}

_CONTEXT_BEFORE = {"my", "his", "her", "their", "our", "your", "with", "match", "matching", "over",
                   "complement", "alongside"}
_ARTICLES = {"a", "an", "the", "this", "that"}
_NEGATION = {"not", "no", "except", "but", "without", "avoid", "than", "nothing", "dont", "hate"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower().replace("'", ""))


def _requested(text: str, table: dict[str, Any]) -> list[str]:
    """Words from `table` the request asks for, in order (not context, not negated)."""
    words = _words(text)
    found: list[str] = []
    for i, word in enumerate(words):
        if word not in table:
            continue
        if word == "off" and (i + 1 >= len(words) or words[i + 1] != "white"):
            continue
        before = words[max(0, i - 3):i]
        if before and (before[-1] in _CONTEXT_BEFORE or (before[-1] in _ARTICLES and len(before) > 1
                                                        and before[-2] in _CONTEXT_BEFORE)):
            continue                                    # "with my red lehenga"
        if any(w in _NEGATION for w in before):
            continue                                    # "anything but black"
        if word not in found:
            found.append(word)
    return found


def colours_asked(text: str) -> list[str]:
    """Colour families the request asks for: "pila kurta" -> ["yellow"]."""
    families: list[str] = []
    for word in _requested(text, COLOURS):
        for family in sorted(COLOURS[word]):
            if family not in families:
                families.append(family)
    return families


def fabrics_asked(text: str) -> list[str]:
    return list(dict.fromkeys(FABRICS[w] for w in _requested(text, FABRICS)))


def fits_asked(text: str) -> list[str]:
    return list(dict.fromkeys(FITS[w] for w in _requested(text, FITS)))


def normalise(values: Any, kind: str) -> list[str]:
    """The assistant's tool arguments ("Black", ["navy blue"], "cotton") as families.
    Anything that is not a known word is dropped, never trusted."""
    if values in (None, "", []):
        return []
    items: Iterable[Any] = values if isinstance(values, list) else [values]
    text = " ".join(str(v) for v in items)
    if kind == "colour":
        return colours_asked(text) or ([text.strip().lower()] if text.strip().lower() in
                                       {f for fs in COLOURS.values() for f in fs} else [])
    if kind == "fabric":
        return fabrics_asked(text)
    return fits_asked(text)


@lru_cache(maxsize=4096)
def _families_of(colour: str, name: str) -> frozenset[str]:
    words = _words(colour or name)
    return frozenset(f for i, w in enumerate(words) if w in COLOURS
                     and (w != "off" or words[i + 1:i + 2] == ["white"]) for f in COLOURS[w])


def product_colours(p: dict[str, Any]) -> frozenset[str]:
    """A product's colour families, from its colour (else its name)."""
    return _families_of(str(p.get("color") or ""), str(p.get("name") or ""))


def _attribute_words(p: dict[str, Any], field: str) -> list[str]:
    return _words(" ".join([str(p.get(field) or ""), " ".join(p.get("tags") or []), str(p.get("name") or "")]))


def product_fabrics(p: dict[str, Any]) -> set[str]:
    return {FABRICS[w] for w in _attribute_words(p, "fabric") if w in FABRICS}


def product_fits(p: dict[str, Any]) -> set[str]:
    return {FITS[w] for w in _attribute_words(p, "fit") if w in FITS}


def fits_request(p: dict[str, Any], colours: list[str], fabrics: list[str], fits: list[str]) -> bool:
    if colours and not set(colours) & product_colours(p):
        return False
    if fabrics and not set(fabrics) & product_fabrics(p):
        return False
    if fits and not set(fits) & product_fits(p):
        return False
    return True
