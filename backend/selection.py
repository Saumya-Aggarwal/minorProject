"""Parse follow-up messages: item selection, cart commands, and buying.

The assistant ends every recommendation with "Reply 1–3 to choose", so "2" has
to mean the second item rather than a fresh search for the string "2". Cart
commands ("ADD", "CART", "REMOVE 2", "SIZE 1 M", "CHECKOUT") work the same way.

The whole message must match a pattern, never just contain one. Substring
matching would wreck ordinary queries: "2 piece kurta set" and "under 3000"
contain digits, and "add a red dupatta to my wishlist" contains "add", but all
three are searches. Anything that does not match falls through to retrieval.
"""

import re
from typing import Any, Optional

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


def _clean(text: str) -> str:
    """Lowercase, drop apostrophes and trailing punctuation, collapse spaces."""
    text = text.strip().lower().replace("'", "").replace("’", "")
    text = text.rstrip(".!?").strip()
    return re.sub(r"\s+", " ", text)


def parse_selection(text: str, count: int) -> Optional[int]:
    """Return a zero-based index into the last shown products, or None.

    None means "not a selection" — the caller should treat the message as a
    new search. Out-of-range numbers return None on purpose: "under 3000" with
    three products shown is a budget, not a choice.
    """
    if count <= 0:
        return None

    cleaned = _clean(text)
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


_PICK_FILLER = {
    "i", "ill", "id", "will", "would", "want", "wanna", "like", "take", "choose", "pick", "select", "go", "with",
    "the", "a", "that", "this", "one", "ones", "please", "pls", "plz", "show", "me", "more", "about", "of",
    "details", "detail", "see", "lets", "let", "us", "ok", "okay", "yes", "yeah", "for", "is", "it", "like",
}


def parse_named_selection(text: str, shown: list[dict[str, Any]]) -> Optional[int]:
    """"choose blush pink one", "the teal one": the one shown product whose name
    contains every remaining word. None unless exactly one matches, and only
    for short messages, so "pink saree under 3000" stays a search."""
    words = re.findall(r"[a-z]+", _clean(text))
    if not shown or not words or len(words) > 8:
        return None
    wanted = [w for w in words if w not in _PICK_FILLER]
    if not wanted:
        return None
    names = [set(re.findall(r"[a-z]+", str(p.get("name", "")).lower())) for p in shown]
    hits = [i for i, name in enumerate(names) if all(w in name for w in wanted)]
    return hits[0] if len(hits) == 1 else None


# --- sizes --------------------------------------------------------------------

# Only tokens that look like a garment size count as one. This is what keeps
# "add a red dupatta" from being read as ADD with size "a red dupatta".
# Shoes are UK sizes: "8", "uk 8" and "UK8" all mean "UK 8" (single digits from 3,
# so "add 2" is not taken as a size).
_SIZE_TOKEN = re.compile(r"^(?:xxs|xs|s|m|l|xl|xxl|xxxl|\d{2}|[3-9]|uk ?\d{1,2}|free|free size|one size)$")


def _shoe(size: str) -> str:
    """'UK 8', 'uk8' and '8' all compare as '8'."""
    return re.sub(r"^uk\s*", "", size.lower())


def match_size(token: str, sizes_available: list[str]) -> Optional[str]:
    """Map what the customer typed to the product's canonical size, or None."""
    token = _clean(token)
    if token in ("free", "one size"):
        token = "free size"
    for size in sizes_available:
        if size.lower() == token or (size.lower().startswith("uk") and _shoe(size) == _shoe(token)):
            return size
    return None


def looks_like_size(token: str) -> bool:
    return bool(_SIZE_TOKEN.match(_clean(token)))


_BARE_SIZE = re.compile(r"^(?:(?:its |it is |i want |i need |in )?size )?(?P<size>.+?)(?: size)?(?: please| pls| plz)?$")


# "i think 11 size would be the best for me", "let's take XXL then": a sentence
# that settles the size. Needs one of these words, so "sherwani in 42 for my
# wedding" stays a search.
_SIZE_CUE = re.compile(r"\b(size|sizes|fit|fits|take|best|prefer|think|suits?|wear|go\s+with|thats\s+my)\b", re.I)


def mentions_size(text: str) -> bool:
    """Worth checking this message against a product's sizes."""
    return bool(_SIZE_CUE.search(_clean(text)))


def find_size_in_text(text: str, sizes_available: list[str]) -> Optional[str]:
    """The one size from this product's list that the sentence names, or None."""
    cleaned = _clean(text)
    if len(cleaned.split()) > 12 or not _SIZE_CUE.search(cleaned):
        return None
    found = {size for size in sizes_available
             if re.search(rf"(?<!\w)(?:uk\s*)?{re.escape(_shoe(size))}(?!\w)", cleaned, re.I)}
    return found.pop() if len(found) == 1 else None


def parse_bare_size(text: str) -> Optional[str]:
    """The size in a reply that is only a size: "xxl", "size 42", "XL please"."""
    match = _BARE_SIZE.match(_clean(text))
    size = match.group("size") if match else ""
    return size if size and looks_like_size(size) else None


# --- cart commands ------------------------------------------------------------

_ADD_PHRASES = {
    "add", "add it", "add this", "add that", "add to cart", "add to my cart",
    "add it to cart", "add it to my cart", "add this to cart", "add this to my cart",
}
_CART_PHRASES = {
    "cart", "my cart", "show cart", "show my cart", "view cart", "view my cart",
    "see cart", "see my cart", "whats in my cart", "what is in my cart",
}
_CHECKOUT_PHRASES = {
    "checkout", "check out", "buy all", "buy everything", "place order",
    "place my order", "pay", "pay now", "proceed to checkout", "proceed to pay",
}
_CLEAR_PHRASES = {"clear cart", "empty cart", "clear my cart", "empty my cart"}
# Not bare "order": that already means "buy the item I just picked"
_ORDERS_PHRASES = {
    "orders", "my orders", "order status", "my order status", "track order",
    "track my order", "track orders", "where is my order", "status", "order history",
}
_HELP_PHRASES = {"help", "menu", "commands", "what can you do", "how does this work"}

# "add", "add this one to cart", "pls add that one to my bag in XXL", "add it in 42"
_ADD_WITH_SIZE = re.compile(
    r"^(?:(?:please|pls|plz|ok|okay|yes|yeah|yep|sure|haan|ha|go ahead and|can you|could you) )*"
    r"add(?: (?:it|this|that)(?: one)?)?"
    r"(?: to (?:my |the )?(?:cart|bag))?(?:(?: in)?(?: size)? (?P<size>.+))?$"
)
_REMOVE = re.compile(r"^(?:remove|delete|drop)(?: item)? (?:no\.? ?|#)?(\d{1,2})$")
_SIZE = re.compile(r"^(?:size|change size)(?: of)?(?: item)? (\d{1,2}) (?:to )?(.+)$")


def parse_command(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """Recognise a cart command, or return None to let the message be a search.

    Returns (name, args):
      ("add", {"size": ""})            ADD, or ADD with a size ("add M")
      ("cart", {})                     CART
      ("remove", {"position": 2})      REMOVE 2
      ("size", {"position": 1, "size": "m"})   SIZE 1 M
      ("checkout", {})                 CHECKOUT / BUY ALL / PAY
      ("clear", {})                    CLEAR CART
    """
    cleaned = _clean(text)
    if not cleaned:
        return None

    if cleaned in _CART_PHRASES:
        return ("cart", {})
    if cleaned in _CHECKOUT_PHRASES:
        return ("checkout", {})
    if cleaned in _CLEAR_PHRASES:
        return ("clear", {})
    if cleaned in _ORDERS_PHRASES:
        return ("orders", {})
    if cleaned in _HELP_PHRASES:
        return ("help", {})
    if cleaned in _ADD_PHRASES:
        return ("add", {"size": ""})

    match = _REMOVE.match(cleaned)
    if match:
        return ("remove", {"position": int(match.group(1))})

    match = _SIZE.match(cleaned)
    if match and looks_like_size(match.group(2)):
        return ("size", {"position": int(match.group(1)), "size": match.group(2)})

    match = _ADD_WITH_SIZE.match(re.sub(r"\s+(?:please|pls|plz)$", "", cleaned))
    if match:
        size = match.group("size")
        if size is None:
            return ("add", {"size": ""})
        if looks_like_size(size):
            return ("add", {"size": size})
        # "add a red dupatta" is a search, not a command

    return None


# --- single-item buy ----------------------------------------------------------

_BUY_PHRASES = {"buy", "buy it", "buy this", "buy now", "order", "order it", "yes buy", "confirm"}
_BUY_WITH_SIZE = re.compile(r"^buy(?: it| this)?(?: in)?(?: size)? (.+)$")


def parse_buy(text: str) -> Optional[str]:
    """Return "" for a plain BUY, the typed size for "BUY M", or None.

    "buy all" and "buy everything" are cart checkouts and are matched by
    parse_command first; this only sees single-item buys.
    """
    cleaned = _clean(text)
    if cleaned in _BUY_PHRASES:
        return ""
    match = _BUY_WITH_SIZE.match(cleaned)
    if match and looks_like_size(match.group(1)):
        return match.group(1)
    return None


# --- reply text ---------------------------------------------------------------


def describe_choice(product: dict, index: int, sizes_available: Optional[list[str]] = None) -> str:
    """Confirm the pick and invite the next step."""
    name = product.get("name", "That item")
    price = product.get("price_inr", product.get("price", 0))
    description = product.get("rich_description") or product.get("description") or ""

    lines = [f"Good choice — {name}, Rs. {price:,.0f}."]
    if description:
        lines.append(description)

    sizes = sizes_available or []
    if len(sizes) > 1:
        example = sizes[len(sizes) // 2]
        lines.append(f"Sizes: {', '.join(sizes)}.")
        lines.append(
            f"Reply ADD {example} to add it to your cart, BUY {example} to order just "
            "this one, or ask to see other options."
        )
    else:
        lines.append(
            "Reply ADD to add it to your cart, BUY to order just this one, "
            "or ask to see other options."
        )
    return "\n".join(lines)


def results_footer(count: int) -> str:
    """Contract C1: Dev A owns the command hints under a list of results."""
    if count <= 0:
        return ""
    choose = "Reply 1 to choose" if count == 1 else f"Reply 1–{count} to choose"
    return f"{choose} · CART to see your cart"


# --- greetings ------------------------------------------------------------------

# "hi" used to run a product search for the word "hi" and answer with three
# random kurtas. Greetings are matched as the whole message, stretched letters
# included ("hiii", "heyyy"), optionally followed by one filler word. Anything
# longer ("hey need a kurta") is a search, so a real request is never swallowed.
_GREETING = re.compile(
    r"^(?:h+i+|h+e+y+|h+e+l+o+|h+l+o+|namaste|namaskar|hola|yo+|start|"
    r"good (?:morning|afternoon|evening))"
    r"(?: (?:there|bot|again|all|everyone|hru|yo+|sir|maam))?$"
)


def is_greeting(text: str) -> bool:
    return bool(_GREETING.match(_clean(text)))


# --- product codes -------------------------------------------------------------

# "Ask about this on WhatsApp" on the website pre-fills a message ending in the
# product code, e.g. "Tell me about the Royal Blue Wedding Sherwani (EW006)".
# A code never occurs in a natural search, so here a substring match is safe.
_PRODUCT_CODE = re.compile(r"\b(EW\d{3})\b", re.IGNORECASE)


def find_product_code(text: str) -> Optional[str]:
    match = _PRODUCT_CODE.search(text)
    return match.group(1).upper() if match else None

