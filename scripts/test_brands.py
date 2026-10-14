"""The bot works for any catalogue, not only the demo's ethnic wear (P2 step 3).

A second store is built from evals/catalogues/fitwear.json (an invented
activewear brand, "Stride Lab", in Shopify's shape) through the real
importer, and checked for:
  * its own vocabulary: tee / t-shirt / tshirts -> T-Shirts, "sports bra"
    settles a woman, gym / yoga / running are occasions, accessories only
    when asked for
  * search that respects it ("sports bra for yoga" shows only sports bras)
  * its own voice: the prompt, greeting, HELP, off-topic line and payment
    description name Stride Lab and what it sells, never Kurta & Co. or
    ethnic wear; first from defaults built from the catalogue, then from its
    brand settings
  * its own product codes (SL001), in chat and in the assistant's replies
And the demo store is unchanged: its prompt is byte-for-byte the one from
before step 3 (scripts/fixtures/demo_system_prompt.txt), and its pack words
still work.

Run from the repo root (Postgres up). Creates and deletes its own store.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    sys.path.insert(0, str(path))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ["LLM_API_KEY"] = ""          # nothing here needs a model

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import select  # noqa: E402

import brand  # noqa: E402
import catalog  # noqa: E402
import checkout  # noqa: E402
import tenancy  # noqa: E402
import vocab  # noqa: E402
from bot import assistant, retrieval  # noqa: E402
from connectors import shopify_public  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from models import Store  # noqa: E402
from routers import webhook  # noqa: E402
from selection import find_product_code  # noqa: E402

SLUG = "stride-lab-test"
CATALOGUE = json.loads((ROOT / "evals" / "catalogues" / "fitwear.json").read_text(encoding="utf-8"))
DEMO_PROMPT = (ROOT / "scripts" / "fixtures" / "demo_system_prompt.txt").read_text(encoding="utf-8")

passed = failed = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def cleanup() -> None:
    with session_scope() as db:
        store = db.exec(select(Store).where(Store.slug == SLUG)).first()
        if store is not None:
            for table in ("product_variants", "products", "stores"):
                db.exec(text(f"DELETE FROM {table} WHERE store_id = :b").bindparams(b=store.store_id))
    tenancy.forget()
    catalog.invalidate()


def make_store() -> int:
    with session_scope() as db:
        store = Store(slug=SLUG, name="Stride Lab (test)", platform="shopify", status="draft", code_prefix="SL",
                      connector={"shop_domain": "stride-lab-test.myshopify.com"})
        db.add(store)
        db.flush()
        sid = store.store_id
    tenancy.forget()

    def fetch(url: str, params: dict) -> httpx.Response:
        start = (params["page"] - 1) * params["limit"]
        return httpx.Response(200, json={"products": CATALOGUE["products"][start:start + params["limit"]]},
                              request=httpx.Request("GET", url, params=params))

    shopify_public.ShopifyPublicConnector(tenancy.get_store(sid), fetch=fetch, sleep=lambda s: None).sync_catalog()
    catalog.invalidate()
    return sid


def set_brand(sid: int, settings: dict) -> None:
    with session_scope() as db:
        db.get(Store, sid).brand = settings
    tenancy.forget()


def categories(query: str) -> list[str]:
    return retrieval.parse_query(query).categories


def main() -> int:
    init_db()
    cleanup()
    try:
        sid = make_store()
        with tenancy.use_store(sid):
            products = catalog.get_all()
            check("the activewear catalogue was imported", len(products) >= 18, len(products))

            print("\n1. Its own vocabulary, from its catalogue")
            words = vocab.current()
            for word in ("tee", "tees", "t-shirt", "tshirts", "t shirt"):
                check(f"{word!r} means T-Shirts", categories(f"black {word} please") == ["T-Shirts"],
                      categories(f"black {word} please"))
            check("'sports bra' means Sports Bras and settles a woman",
                  retrieval.parse_query("sports bra for yoga").categories == ["Sports Bras"]
                  and retrieval.parse_query("sports bra for yoga").gender == "Women",
                  retrieval.parse_query("sports bra for yoga"))
            check("'joggers for my brother': Joggers, for a man",
                  categories("joggers for my brother") == ["Joggers"]
                  and retrieval.parse_query("joggers for my brother").gender == "Men")
            check("hoodie, leggings, tank top and shorts are its categories",
                  [categories(w) for w in ("hoodie", "leggings", "tank top", "shorts")]
                  == [["Hoodies"], ["Leggings"], ["Tank Tops"], ["Shorts"]],
                  [categories(w) for w in ("hoodie", "leggings", "tank top", "shorts")])
            check("gym, yoga and running are occasions (from Shopify tags)",
                  {"gym", "yoga", "running"} <= words.occasions, sorted(words.occasions))
            check("an occasion is read from the request",
                  retrieval.parse_query("something for running").occasion == "running")
            check("Accessories are an add-on, shown only when asked for", words.accessories == {"Accessories"},
                  words.accessories)
            check("no ethnic-wear words leak in", not {"kurta", "saree", "sherwani", "haldi"}
                  & (set(words.garments) | words.occasions))

            print("\n2. Search keeps to its catalogue")
            shown = retrieval.search("sports bra for yoga").products
            check("'sports bra for yoga' shows only sports bras",
                  shown and all(p["category"] == "Sports Bras" for p in shown), [p["name"] for p in shown])
            shown = retrieval.search("tee for the gym", k=6).products
            check("'tee for the gym' shows tees, gym-tagged first, no accessories",
                  shown and all(p["category"] == "T-Shirts" for p in shown) and "gym" in shown[0]["tags"],
                  [(p["name"], p["tags"]) for p in shown])
            shown = retrieval.search("gym bag").products
            check("'gym bag' does show the bag", any(p["category"] == "Accessories" for p in shown),
                  [p["name"] for p in shown])
            shown = retrieval.search("black oversized tee", k=6).products
            check("'black oversized tee': black (Shopify's colour option), oversized (its tags)",
                  shown and all(p["color"] == "Black" and "oversized" in p["tags"] for p in shown),
                  [(p["name"], p["color"], p["tags"]) for p in shown])
            shown = retrieval.search("navy sports bra", k=6).products
            check("'navy sports bra': only the navy one", shown and all(p["color"] == "Navy" and
                                                                       p["category"] == "Sports Bras" for p in shown),
                  [p["name"] for p in shown])
            shown = retrieval.search("something comfy for running", k=6).products
            check("an open request never shows socks or caps first",
                  shown and shown[0]["category"] != "Accessories", [p["name"] for p in shown])

            print("\n3. Its own voice, with no brand settings yet")
            shop = brand.current()
            prompt = brand.system_prompt()
            check("the shop is named after the store", shop.name == "Stride Lab (test)", shop.name)
            check("the prompt says what it sells, from the catalogue",
                  "t-shirts" in prompt and "sports bras" in prompt and "Women:" in prompt, prompt[:400])
            check("nothing of the demo in the prompt",
                  not any(w in prompt.lower() for w in ("kurta", "ethnic", "haldi", "sherwani", "saree")))
            check("the greeting names it", "Stride Lab (test) shopping assistant" in webhook._greeting(None, []),
                  webhook._greeting(None, []))
            check("HELP's examples are its own categories", '"t-shirts"' in webhook.help_text(),
                  webhook.help_text().splitlines()[1])
            check("the off-topic line names it", "Stride Lab (test)" in shop.off_topic, shop.off_topic)
            check("the payment description names it",
                  checkout._describe({"order_id": 7, "items": [{"product_name": "Zip Hoodie"}]})
                  == "Stride Lab (test) order #7: Zip Hoodie")

            print("\n4. Its brand settings win")
            set_brand(sid, {"name": "Stride Lab", "description": "an activewear brand for the gym, running and yoga",
                            "scope": "activewear", "help_examples": ['"black oversized tee"', '"leggings for yoga"'],
                            "policies": "free delivery over Rs 999; easy 7-day exchanges"})
            shop, prompt = brand.current(), brand.system_prompt()
            check("its name and description lead the prompt",
                  prompt.startswith("You are the shopping assistant for Stride Lab, an activewear brand"), prompt[:120])
            check("its policies reach the rules", "free delivery over Rs 999" in prompt)
            check("off-topic says what it helps with", shop.off_topic.startswith("I only help with activewear"),
                  shop.off_topic)
            check("the greeting uses its examples", '"black oversized tee" or "leggings for yoga"'
                  in webhook._greeting(None, []), webhook._greeting(None, []))

            print("\n5. Its own product codes")
            code = products[0]["id"]
            check("its codes carry its prefix", code.startswith("SL"), code)
            check("a code typed in chat finds its product", find_product_code(f"send me {code} pls") == code)
            check("the assistant removes its codes from what it says",
                  code not in assistant._strip_codes(f"Try {code}, it is light.") and "SL999" in
                  assistant._strip_codes("SL999 is not ours"))
            reply, _ = assistant._finish({"message": "Two picks.", "product_ids": f"{products[0]['id']}, {products[1]['id']}"},
                                         "tee")
            check("product ids written as text are read", reply is not None and len(reply.products) == 2,
                  reply and [p["id"] for p in reply.products])

        print("\n6. The demo store is unchanged")
        with tenancy.use_store(tenancy.DEMO_STORE_ID):
            check("its prompt is byte-for-byte the one from before step 3", brand.system_prompt() == DEMO_PROMPT)
            check("its pack words still work: 'kurta for my wife' -> kurtis and suits",
                  categories("kurta for my wife") == ["Kurti", "Salwar Suit", "Co-ord Set"],
                  categories("kurta for my wife"))
            check("and haldi is still an occasion", retrieval.parse_query("haldi outfit").occasion == "haldi")
            check("its greeting is Kurta & Co.'s", "Kurta & Co. shopping assistant" in webhook._greeting(None, [])
                  and '"sherwani for my wedding"' in webhook._greeting(None, []))
            check("its off-topic line is unchanged", brand.current().off_topic.startswith(
                "I only help with our ethnic wear"))
            check("the activewear words mean nothing here", categories("sports bra") == [])
    finally:
        cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
