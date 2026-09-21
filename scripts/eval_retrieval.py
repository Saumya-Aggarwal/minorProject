"""Measure retrieval against a fixed set of real customer phrasings.

Evaluates bot/retrieval.py directly (no LLM), so ranking and filter regressions
show up even when the model is unavailable. A case passes only when EVERY
product returned satisfies it — "one of three was right" hides the misses a
customer actually sees — and when the result is not empty unless it should be.

History: the first version of this script scored 8/10 with the filters in the
old bot/chat.py, and the two misses returned nothing at all ("jacket to wear
over my kurta", "bandhgala for a formal event": category "Jacket" does not
exist in the catalogue).

Run from the repo root:  backend/.venv/Scripts/python scripts/eval_retrieval.py
"""

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from bot import retrieval  # noqa: E402


@dataclass(frozen=True)
class Case:
    query: str
    categories: tuple[str, ...] = ()
    gender: str | None = None
    max_price: float | None = None
    min_price: float | None = None
    both_genders: bool = False      # results must include men's AND women's pieces
    empty: bool = False             # nothing should come back
    relaxed: str | None = None      # this filter must be reported as relaxed


CASES = (
    # The misses found when testing the first RAG version
    Case("jacket to wear over my kurta", categories=("Nehru Jacket", "Waistcoat")),
    Case("bandhgala for a formal event", categories=("Jodhpuri Suit",), gender="Men"),
    Case("kurta for my wife", categories=("Kurti", "Salwar Suit"), gender="Women"),
    Case("sherwani under 3000", categories=("Sherwani",), relaxed="budget"),
    Case("me and my wife need wedding outfits", both_genders=True),
    Case("asdfgh qwerty", empty=True),
    # Everyday requests
    Case("sherwani for my wedding", categories=("Sherwani",), gender="Men"),
    Case("saree for office", categories=("Saree",), gender="Women"),
    Case("saree under 3000", categories=("Saree",), max_price=3000),
    Case("dupatta under 2000", categories=("Dupatta",), max_price=2000),
    Case("men's kurta for a festival", categories=("Kurta", "Kurta Set"), gender="Men"),
    Case("women's lehenga for a wedding", categories=("Lehenga",), gender="Women"),
    Case("office kurta under 2500", categories=("Kurta", "Kurta Set", "Kurti"), max_price=2500),
    Case("something for my sister's mehendi", gender="Women"),
    Case("outfit for my husband's cousin's sangeet", gender="Men"),
    # Budgets in the forms people type them
    Case("kurta under 2k", max_price=2000),
    Case("saree below rs 1000", categories=("Saree",), relaxed="budget"),
    Case("lehenga between 5000 and 10000", categories=("Lehenga",), min_price=5000, max_price=10000),
    Case("something around ₹3000 for my brother", gender="Men", max_price=3450),
    Case("kurti within 1500", categories=("Kurti",), max_price=1500),
    # Wording the old keyword filters did not know
    Case("nehru jacket for diwali", categories=("Nehru Jacket", "Waistcoat")),
    Case("sharara for haldi", categories=("Salwar Suit",), gender="Women"),
    Case("churidar to go with my kurta", categories=("Bottomwear",)),
    Case("dupatta for men", categories=("Stole",), gender="Men"),
    Case("saree for my husband", gender="Men", relaxed="category"),
    Case("matching outfits for the couple", both_genders=True),
)


def judge(case: Case, result: retrieval.SearchResult) -> tuple[bool, str]:
    products = result.products
    if case.empty:
        return (not products, "expected nothing")
    if not products:
        return (False, "returned nothing")
    if case.relaxed and case.relaxed not in result.relaxed:
        return (False, f"expected '{case.relaxed}' relaxed, got {result.relaxed}")
    for p in products:
        if case.categories and p["category"] not in case.categories:
            return (False, f"{p['name']} is a {p['category']}")
        if case.gender and p["gender"] != case.gender:
            return (False, f"{p['name']} is {p['gender']}")
        if case.max_price and not case.relaxed and p["price"] > case.max_price:
            return (False, f"{p['name']} costs {p['price']}")
        if case.min_price and not case.relaxed and p["price"] < case.min_price:
            return (False, f"{p['name']} costs {p['price']}")
    if case.both_genders and len({p["gender"] for p in products}) < 2:
        return (False, "only one gender shown")
    return (True, "")


def evaluate() -> int:
    passed = 0
    for case in CASES:
        result = retrieval.search(case.query)
        ok, why = judge(case, result)
        passed += ok
        shown = ", ".join(f"{p['name']} ({p['gender'][0]}, Rs {p['price']:.0f})" for p in result.products)
        extra = f"  [relaxed: {', '.join(result.relaxed)}]" if result.relaxed else ""
        print(f"{'PASS' if ok else 'FAIL'} | {case.query} | {shown or '—'}{extra}{'  <- ' + why if why else ''}")
    print(f"\nHit rate: {passed}/{len(CASES)} ({passed / len(CASES):.0%})")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(evaluate())
