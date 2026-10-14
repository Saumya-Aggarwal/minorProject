"""Grading one turn by code: what the case expects, plus checks every turn gets.

A turn is graded from facts, never from how a reply sounds:
  * the trace (tracing.py): which exit answered, tools, guards, the reply as sent
  * the customer's state before and after (state.snapshot): cart, orders,
    the list on screen, the item picked, profile, saved address
  * the catalogue: real sizes, prices, genders, categories, colours

Expectations a case may set on a turn (all optional; see evals/README.md):
  path, cart, cart_has, cart_lacks, cart_unchanged, cart_units, selected,
  shows, shows_min, shows_max, shows_any, shows_none, shown_fit, asks,
  reply_has, reply_lacks, cta, order_placed, address_pending, profile_gender,
  hidden_has

Universal checks (every graded turn; a turn can `skip:` one by name):
  replied, claims_backed, pay_claim_backed, sizes_real, amounts_real,
  no_fragments, no_code, no_markdown, no_errors, no_fallback, cart_shown_once

What only judgement can settle (tone, whether advice was sensible) is the
`judge` rubric, graded by a model in judge.py, reported apart from these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

UNIVERSAL = ("replied", "claims_backed", "pay_claim_backed", "sizes_real", "amounts_real",
             "no_fragments", "no_code", "no_markdown", "no_errors", "no_fallback", "cart_shown_once")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    universal: bool = False


@dataclass
class Turn:
    """Everything known about one graded turn."""

    say: str                                   # the customer's words ("" for media)
    reply: dict[str, Any]                      # tracing.describe(): text, cards, cta_url ...
    trace: dict[str, Any]                      # path, tools, guards, llm_calls, error ...
    before: dict[str, Any]                     # state.snapshot before the turn
    after: dict[str, Any]
    products: dict[str, dict[str, Any]]        # the store's catalogue by id
    customer_said: str = ""                    # all the customer's words so far (for amounts, sizes)
    extra_amounts: set[int] = field(default_factory=set)


# --- helpers ---------------------------------------------------------------------------

def _text(turn: Turn) -> str:
    return str(turn.reply.get("text") or "")


def _shown(turn: Turn) -> list[str]:
    return list(turn.reply.get("product_ids") or [])


def _norm_size(size: Any) -> str:
    return re.sub(r"\s+", " ", str(size or "")).strip().upper()


def _lines(cart: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for line in cart:
        key = (line["product"], _norm_size(line.get("size")))
        out[key] = out.get(key, 0) + int(line.get("qty") or 0)
    return out


def _line_matches(line: dict[str, Any], want: dict[str, Any]) -> bool:
    if line["product"] != want["product"]:
        return False
    if "size" in want and _norm_size(line.get("size")) != _norm_size(want["size"]):
        return False
    if "qty" in want and int(line.get("qty") or 0) != int(want["qty"]):
        return False
    return True


def _fmt_cart(cart: list[dict[str, Any]]) -> str:
    return ", ".join(f"{l['product']}/{l.get('size') or '-'}x{l.get('qty')}" for l in cart) or "empty"


def _search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE | re.DOTALL) is not None


# --- what a case expects --------------------------------------------------------------

def expected(turn: Turn, expect: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    add = lambda name, ok, detail="": checks.append(Check(name, bool(ok), detail if not ok else ""))  # noqa: E731
    text, shown = _text(turn), _shown(turn)
    after_cart, before_cart = turn.after["cart"], turn.before["cart"]

    if "path" in expect:
        allowed = expect["path"] if isinstance(expect["path"], list) else [expect["path"]]
        path = turn.trace.get("path") or ""
        add("path", any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in allowed),
            f"path {path!r}, expected {allowed}")

    if "cart" in expect:
        want = expect["cart"] or []
        ok = len(want) == len(after_cart) and all(any(_line_matches(l, w) for l in after_cart) for w in want)
        add("cart", ok, f"cart is {_fmt_cart(after_cart)}, expected {_fmt_cart([{**w, 'qty': w.get('qty', '?')} for w in want])}")
    for want in expect.get("cart_has") or []:
        add("cart_has", any(_line_matches(l, want) for l in after_cart),
            f"no {want} in cart ({_fmt_cart(after_cart)})")
    for product in expect.get("cart_lacks") or []:
        add("cart_lacks", not any(l["product"] == product for l in after_cart), f"{product} is in the cart")
    if "cart_unchanged" in expect:
        same = _lines(after_cart) == _lines(before_cart)
        add("cart_unchanged", same == bool(expect["cart_unchanged"]),
            f"cart went from {_fmt_cart(before_cart)} to {_fmt_cart(after_cart)}")
    if "cart_units" in expect:
        add("cart_units", turn.after["cart_units"] == expect["cart_units"],
            f"{turn.after['cart_units']} units, expected {expect['cart_units']}")

    if "selected" in expect:
        add("selected", turn.after["session"]["selected"] == expect["selected"],
            f"selected {turn.after['session']['selected']!r}, expected {expect['selected']!r}")

    if "shows" in expect:
        add("shows", bool(shown) == bool(expect["shows"]),
            f"showed {shown or 'nothing'}" + (", expected products" if expect["shows"] else ", expected none"))
    if "shows_min" in expect:
        add("shows_min", len(shown) >= expect["shows_min"], f"showed {len(shown)}, expected >= {expect['shows_min']}")
    if "shows_max" in expect:
        add("shows_max", len(shown) <= expect["shows_max"], f"showed {len(shown)}, expected <= {expect['shows_max']}")
    if expect.get("shows_any"):
        add("shows_any", bool(set(shown) & set(expect["shows_any"])),
            f"showed {shown or 'nothing'}, none of {expect['shows_any']}")
    if expect.get("shows_none"):
        bad = [p for p in shown if p in set(expect["shows_none"])]
        add("shows_none", not bad, f"showed {bad}")
    if expect.get("shown_fit"):
        misfits = [f"{pid} ({why})" for pid in shown
                   for why in [misfit(turn.products.get(pid), expect["shown_fit"])] if why]
        add("shown_fit", not misfits, "; ".join(misfits))

    if "asks" in expect:
        asks = "?" in text
        add("asks", asks == bool(expect["asks"]), "asked nothing" if expect["asks"] else "asked a question")
    for pattern in _as_list(expect.get("reply_has")):
        add("reply_has", _search(pattern, text), f"no /{pattern}/ in reply")
    for pattern in _as_list(expect.get("reply_lacks")):
        add("reply_lacks", not _search(pattern, text), f"/{pattern}/ in reply")

    if "cta" in expect:
        has = bool(turn.reply.get("cta_url"))
        add("cta", has == bool(expect["cta"]), "no Pay Now button" if expect["cta"] else "sent a Pay Now button")
    if "order_placed" in expect:
        placed = len(turn.after["orders"]) > len(turn.before["orders"])
        add("order_placed", placed == bool(expect["order_placed"]),
            "no order created" if expect["order_placed"] else "an order was created")
    if "address_pending" in expect:
        pending = turn.after["session"]["pending_address"] is not None
        add("address_pending", pending == bool(expect["address_pending"]),
            "not asking for an address" if expect["address_pending"] else "still collecting an address")
    if "profile_gender" in expect:
        add("profile_gender", turn.after["profile"]["gender"] == expect["profile_gender"],
            f"profile gender {turn.after['profile']['gender']!r}, expected {expect['profile_gender']!r}")
    for product in expect.get("hidden_has") or []:
        add("hidden_has", product in turn.after["hidden"], f"{product} not hidden")
    return checks


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def misfit(product: Optional[dict[str, Any]], fit: dict[str, Any]) -> str:
    """Why a shown product does not meet `shown_fit`, or "" if it does."""
    if product is None:
        return "not in the catalogue"
    gender = fit.get("gender")
    if gender and product.get("gender") not in (gender, "Unisex"):
        return product.get("gender") or "no gender"
    categories = fit.get("categories")
    if categories and product.get("category") not in categories:
        return product.get("category") or "no category"
    if fit.get("max_price") is not None and product["price"] > fit["max_price"]:
        return f"Rs {product['price']:.0f}"
    if fit.get("min_price") is not None and product["price"] < fit["min_price"]:
        return f"Rs {product['price']:.0f}"
    occasions = _as_list(fit.get("occasion"))
    if occasions:
        tags = " | ".join(product.get("occasion") or []).lower()
        if not any(o.lower() in tags for o in occasions):
            return "occasions " + ", ".join(product.get("occasion") or [])
    colours = _as_list(fit.get("colour"))
    if colours:
        # The catalogue's key is "color" (the scraped-dataset field names)
        named = product.get("color") or product.get("colour") or ""
        colour = f"{named} {product.get('name') or ''}".lower()
        if not any(c.lower() in colour for c in colours):
            return f"colour {named or 'unknown'}"
    return ""


# --- what every turn gets -------------------------------------------------------------

_CLAIMS_ADD = re.compile(
    r"\b(?:i(?:'ve| have)?\s+added|added\b(?!\s+to\s+(?:the\s+)?(?:list|wishlist))|i'?ll\s+add|i\s+will\s+add"
    r"|adding\s+(?:it|this|that|the)|(?:is|are)\s+now\s+in\s+your\s+(?:cart|bag)|now\s+in\s+your\s+cart)",
    re.IGNORECASE)
_CLAIMS_REMOVE = re.compile(r"\b(?:i(?:'ve| have)?\s+removed|removed\b|i'?ll\s+remove|taken\s+out|dropped)",
                            re.IGNORECASE)
_PAY_CLAIM = re.compile(r"pay\s*now\s+button|tap\s+(?:the\s+)?pay\s*now|here'?s\s+your\s+(?:secure\s+)?(?:pay|payment)"
                        r"|payment\s+link\s+(?:below|above|here)", re.IGNORECASE)
_AMOUNT = re.compile(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand)?", re.IGNORECASE)
# "Added the." / "...for your sister's haldi. The." ("delivering to:" before an
# address is fine, and so is a preposition at the end: "what you're looking for!")
_FRAGMENT = re.compile(r"(?:^|[\s(])(?:the|a|an|and|of|your|our)\s*[.!](?=\s|$)", re.IGNORECASE | re.M)
_CODE = re.compile(r"```|\b(?:print|console\.log)\s*\(|\bdef\s+\w+\s*\(|^\s*import\s+\w+", re.IGNORECASE | re.M)
_MARKDOWN = re.compile(r"\*\*[^*]+\*\*|^\s*#{1,6}\s|\[[^\]]+\]\(https?://", re.M)
# The cart block's own header. Live (reply 580) it came twice; "Your cart has 4
# items: 3 sarees and 1 kurta" above one block is an answer, not a second copy
_CART_BLOCK = re.compile(r"your cart:", re.IGNORECASE)

_SIZE_WORDS = {"small": "S", "medium": "M", "large": "L", "extra large": "XL", "free size": "FREE SIZE",
               "one size": "FREE SIZE"}
# What the customer typed: any case ("xxl")
_SIZE_TOKEN = re.compile(r"\b(?:XXXL|XXL|XL|XS|S|M|L|UK\s?\d{1,2}|\d{2}|free size|one size|extra large|small|medium|large)\b",
                         re.IGNORECASE)
# What the bot offers: letter sizes only in capitals, or the "s" of "It's" is a size S
_LETTER_SIZE = re.compile(r"\b(?:XXXL|XXL|XL|XS|S|M|L)\b")
_OTHER_SIZE = re.compile(r"\b(?:UK\s?\d{1,2}|\d{2}|free size|one size|extra large|small|medium|large)\b", re.IGNORECASE)
_SIZE_CONTEXT = re.compile(r"\bsizes?\b|\bin\s+(?:a\s+)?(?:small|medium|large|xs|s|m|l|xl|xxl|uk\s?\d+)\b",
                           re.IGNORECASE)


def _value(number: str, unit: Optional[str]) -> int:
    value = float(number.replace(",", ""))
    return round(value * 1000 if unit else value)


def _size_key(token: str) -> str:
    token = re.sub(r"\s+", " ", token.strip()).lower()
    if token in _SIZE_WORDS:
        return _SIZE_WORDS[token]
    if re.fullmatch(r"uk ?\d{1,2}", token):
        return "UK " + re.sub(r"\D", "", token)
    return token.upper()


def _in_focus(turn: Turn) -> list[dict[str, Any]]:
    """Products this reply can be talking about: on screen, picked, in the cart, or named."""
    ids = set(_shown(turn)) | {turn.after["session"]["selected"], turn.before["session"]["selected"]}
    ids |= set(turn.before["session"]["shown"]) | {l["product"] for l in turn.after["cart"]}
    text = _text(turn).lower()
    ids |= {pid for pid, p in turn.products.items() if p["name"].lower() in text}
    return [turn.products[pid] for pid in ids if pid in turn.products]


def sizes_offered(text: str) -> list[str]:
    """Size tokens the reply states as sizes (only in a sentence about size)."""
    found: list[str] = []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        if not _SIZE_CONTEXT.search(sentence):
            continue
        for token in _LETTER_SIZE.findall(sentence) + _OTHER_SIZE.findall(sentence):
            if re.fullmatch(r"\d{2}", token) and not 26 <= int(token) <= 50:
                continue
            found.append(_size_key(token))
    return found


def universal(turn: Turn) -> list[Check]:
    text = _text(turn)
    checks: list[Check] = []
    add = lambda name, ok, detail="": checks.append(Check(name, bool(ok), detail if not ok else "", True))  # noqa: E731

    add("replied", bool(text.strip() or _shown(turn)), "no reply")

    before, after = _lines(turn.before["cart"]), _lines(turn.after["cart"])
    grew = any(after.get(k, 0) > before.get(k, 0) for k in after)
    shrank = any(after.get(k, 0) < before.get(k, 0) for k in before)
    claims = []
    if _CLAIMS_ADD.search(text) and not grew and not re.search(r"already in your cart", text, re.I):
        claims.append("says it added, cart did not grow")
    if _CLAIMS_REMOVE.search(text) and not shrank:
        claims.append("says it removed, cart did not shrink")
    add("claims_backed", not claims, "; ".join(claims))

    add("pay_claim_backed", not (_PAY_CLAIM.search(text) and not turn.reply.get("cta_url")),
        "talks about a Pay Now button that was not sent")

    offered = sizes_offered(text)
    focus = _in_focus(turn)
    if offered and focus:
        real = {_size_key(s) for p in focus for s in (p.get("sizes_available") or [])}
        said = {_size_key(t) for t in _SIZE_TOKEN.findall(turn.say or "")}
        invented = sorted({s for s in offered if s not in real and s not in said})
        add("sizes_real", not invented, f"sizes {invented} are not sizes of {[p['id'] for p in focus][:6]}")
    else:
        add("sizes_real", True)

    known = {round(p["price"]) for p in turn.products.values()} | {round(p.get("mrp") or 0) for p in turn.products.values()}
    for snap in (turn.before, turn.after):
        known |= {round(snap["cart_total"])} | {round(l["line_total"]) for l in snap["cart"]}
        known |= {round(o["total"]) for o in snap["orders"]}
    known |= {_value(n, u) for n, u in _NUMBER.findall(turn.customer_said)}
    known |= turn.extra_amounts
    wrong = sorted({f"Rs {n}{u}" for n, u in _AMOUNT.findall(text) if _value(n, u) not in known})
    add("amounts_real", not wrong, f"{wrong} match no price, total or number the customer gave")

    fragments = [m.group(0).strip() for m in _FRAGMENT.finditer(text)]
    add("no_fragments", not fragments, f"cut sentence(s): {fragments}")
    add("no_code", not _CODE.search(text), "reply contains code")
    add("no_markdown", not _MARKDOWN.search(text), "reply contains markdown")

    swallowed = [g for g in turn.trace.get("guards") or [] if g.get("name") == "swallowed"]
    errors = ([turn.trace["error"]] if turn.trace.get("error") else []) + [g.get("where", "") for g in swallowed]
    add("no_errors", not errors and turn.trace.get("path") != "error", f"errors: {errors}")

    fallback = [g for g in turn.trace.get("guards") or [] if g.get("name") in ("fallback", "model_dropped")]
    add("no_fallback", not fallback, "; ".join(f"{g['name']}: {g.get('reason') or g.get('error') or ''}"[:160]
                                                for g in fallback))

    add("cart_shown_once", len(_CART_BLOCK.findall(text)) <= 1, "the cart is shown more than once")
    return checks


def grade(turn: Turn, expect: dict[str, Any], skip: list[str]) -> list[Check]:
    return [c for c in universal(turn) if c.name not in skip] + expected(turn, expect or {})


def judged(turn: Turn, rubric: str, judge: Optional[Callable[[str, str, str], tuple[bool, str]]]) -> Optional[Check]:
    if not rubric or judge is None:
        return None
    ok, why = judge(rubric, turn.say, _text(turn))
    return Check("judge", ok, why)
