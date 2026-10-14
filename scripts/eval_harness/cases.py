"""Case files: loading, checking, and the placeholders turns may use.

evals/cases/<brand>/*.yaml, each a list of cases. The format is described in
evals/README.md. Everything is checked when loaded, before any model is called:
an unknown key, a product the store does not sell or a size it does not come
in fails loudly instead of quietly grading nonsense.

Placeholders, filled in just before a turn runs:
  {order1} {order2} ...   ids of the orders seeded by `given.orders`
  {shown1} {shown2} ...   the products on screen before this turn (numbered list)
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

CASE_KEYS = {"id", "source", "tags", "given", "prelude", "turns", "note"}
GIVEN_KEYS = {"name", "linked", "profile", "address", "orders", "cart"}
TURN_KEYS = {"say", "button", "media", "model", "expect", "skip", "judge", "note"}
EXPECT_KEYS = {"path", "cart", "cart_has", "cart_lacks", "cart_unchanged", "cart_units", "selected", "shows",
               "shows_min", "shows_max", "shows_any", "shows_none", "shown_fit", "asks", "reply_has",
               "reply_lacks", "cta", "order_placed", "address_pending", "profile_gender", "hidden_has"}
FIT_KEYS = {"gender", "categories", "max_price", "min_price", "occasion", "colour"}
TOOLS = {"send_reply", "search_products", "get_product_details", "get_my_orders", "get_my_cart", "add_to_cart",
         "remove_from_cart", "hide_product", "text"}
_PLACEHOLDER = re.compile(r"\{(order|shown)(\d+)\}")


class CaseError(ValueError):
    pass


@dataclass
class Case:
    id: str
    brand: str
    file: str
    tags: list[str]
    given: dict[str, Any]
    prelude: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def load(cases_dir: Path, brand: str, products: dict[str, dict[str, Any]],
         only: Optional[list[str]] = None, tags: Optional[list[str]] = None) -> list[Case]:
    folder = cases_dir / brand
    if not folder.is_dir():
        raise CaseError(f"no cases for brand {brand!r} in {folder}")
    cases: list[Case] = []
    seen: set[str] = set()
    for path in sorted(folder.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise CaseError(f"{path.name}: not valid YAML: {exc}") from exc
        if not isinstance(data, list):
            raise CaseError(f"{path.name}: a case file is a list of cases")
        for raw in data:
            case = _case(raw, brand, path.name, products)
            if case.id in seen:
                raise CaseError(f"{path.name}: duplicate case id {case.id!r}")
            seen.add(case.id)
            cases.append(case)
    if only:
        cases = [c for c in cases if any(fnmatch.fnmatch(c.id, pattern) for pattern in only)]
    if tags:
        cases = [c for c in cases if set(tags) & set(c.tags)]
    return cases


def _case(raw: Any, brand: str, file: str, products: dict[str, dict[str, Any]]) -> Case:
    where = f"{file}: {raw.get('id', '?') if isinstance(raw, dict) else '?'}"
    if not isinstance(raw, dict) or not raw.get("id"):
        raise CaseError(f"{where}: every case needs an id")
    _keys(raw, CASE_KEYS, where)
    given = raw.get("given") or {}
    _keys(given, GIVEN_KEYS, where + " given")
    for line in given.get("cart") or []:
        _product(line, products, where + " given.cart")
    for order in given.get("orders") or []:
        for line in order.get("items") or []:
            _product(line, products, where + " given.orders")
    if not raw.get("turns"):
        raise CaseError(f"{where}: no turns to grade")
    for kind, turns in (("prelude", raw.get("prelude") or []), ("turns", raw["turns"])):
        for n, turn in enumerate(turns, 1):
            _turn(turn, products, f"{where} {kind}[{n}]", graded=kind == "turns")
    return Case(id=str(raw["id"]), brand=brand, file=file, tags=list(raw.get("tags") or []), given=given,
                prelude=list(raw.get("prelude") or []), turns=list(raw["turns"]),
                source=str(raw.get("source") or ""), raw=raw)


def _keys(data: Any, allowed: set[str], where: str) -> None:
    if not isinstance(data, dict):
        raise CaseError(f"{where}: expected a mapping")
    unknown = set(data) - allowed
    if unknown:
        raise CaseError(f"{where}: unknown key(s) {sorted(unknown)}; allowed {sorted(allowed)}")


def _product(line: dict[str, Any], products: dict[str, dict[str, Any]], where: str) -> None:
    product = products.get(line.get("product", ""))
    if product is None:
        raise CaseError(f"{where}: {line.get('product')!r} is not a product of this store")
    size = line.get("size")
    if size and size not in (product.get("sizes_available") or []):
        raise CaseError(f"{where}: {product['id']} has no size {size!r} ({product.get('sizes_available')})")


def _turn(turn: Any, products: dict[str, dict[str, Any]], where: str, graded: bool) -> None:
    _keys(turn, TURN_KEYS, where)
    kinds = [k for k in ("say", "button", "media") if k in turn]
    if len(kinds) != 1:
        raise CaseError(f"{where}: a turn has exactly one of say / button / media")
    if not graded and set(turn) & {"expect", "skip", "judge"}:
        raise CaseError(f"{where}: prelude turns are not graded (no expect/skip/judge)")
    for step in turn.get("model") or []:
        for call in step if isinstance(step, list) else [step]:
            if not isinstance(call, dict) or len(call) != 1 or next(iter(call)) not in TOOLS:
                raise CaseError(f"{where}: a model step is {{tool: args}} or {{text: ...}}, one of {sorted(TOOLS)}")
    expect = turn.get("expect") or {}
    _keys(expect, EXPECT_KEYS, where + " expect")
    _keys(expect.get("shown_fit") or {}, FIT_KEYS, where + " shown_fit")
    from .checks import UNIVERSAL
    for name in turn.get("skip") or []:
        if name not in UNIVERSAL:
            raise CaseError(f"{where}: skip {name!r} is not a universal check ({UNIVERSAL})")
    ids = [p for key in ("shows_any", "shows_none", "cart_lacks", "hidden_has") for p in expect.get(key) or []]
    ids += [line["product"] for key in ("cart", "cart_has") for line in expect.get(key) or []]
    ids += [expect["selected"]] if expect.get("selected") else []
    for pid in ids:
        if not _PLACEHOLDER.fullmatch(str(pid)) and pid not in products:
            raise CaseError(f"{where}: {pid!r} is not a product of this store")


def fill(value: Any, orders: list[int], shown: list[Optional[str]]) -> Any:
    """Replace {orderN} / {shownN} in strings, recursively."""
    if isinstance(value, str):
        def swap(match: re.Match) -> str:
            n = int(match.group(2)) - 1
            pool = [str(o) for o in orders] if match.group(1) == "order" else [s or "" for s in shown]
            if not 0 <= n < len(pool):
                raise CaseError(f"{match.group(0)} used but only {len(pool)} {match.group(1)}(s) exist")
            return pool[n]
        return _PLACEHOLDER.sub(swap, value)
    if isinstance(value, list):
        return [fill(v, orders, shown) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, orders, shown) for k, v in value.items()}
    return value
