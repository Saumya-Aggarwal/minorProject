"""Two stores never see each other's customers, carts, orders, products,
vectors or training — and the Shopify importer builds a store correctly.

Store B is made from scripts/fixtures/shopify_products.json (an invented
brand in Shopify's /products.json format) through the real importer, with
the network replaced by the fixture. Everything B creates is deleted at the
start and end; the demo store's own data is never touched.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_tenancy.py
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Rs sign in output; a Windows pipe is cp1252

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
os.environ["LLM_API_KEY"] = ""          # plain search: deterministic, no quota
os.environ["OPENAI_API_KEY"] = ""
os.environ.setdefault("MUNIM_SECRET_KEYS", "")

import httpx  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import select  # noqa: E402

import catalog  # noqa: E402
import repository as repo  # noqa: E402
import search  # noqa: E402
import secrets_box  # noqa: E402
import tenancy  # noqa: E402
import training  # noqa: E402
from bot import retrieval  # noqa: E402
from connectors import shopify_public  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import Order, Product, ProductVariant, Store, User  # noqa: E402
from selection import find_product_code  # noqa: E402

DEMO = tenancy.DEMO_STORE_ID
SLUG = "test-tenant-b"
PNID_B = "TEST-PNID-B"
PHONE = "910000000077"            # the same person messaging both brands
TOKEN_B = "EAAG-test-token-for-store-b"
QUESTION_MARK = "tenancy test"    # in every owner rating this suite makes, for cleanup
FIXTURE = json.loads((ROOT / "scripts" / "fixtures" / "shopify_products.json").read_text(encoding="utf-8"))

passed = failed = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


def _delete_users(db, where: str, params: dict) -> None:
    users = f"SELECT user_id FROM users WHERE {where}"
    for statement in (
        f"DELETE FROM order_items WHERE order_id IN (SELECT order_id FROM orders WHERE user_id IN ({users}))",
        f"DELETE FROM orders WHERE user_id IN ({users})",
        f"DELETE FROM cart_items WHERE user_id IN ({users})",
        f"DELETE FROM sessions WHERE user_id IN ({users})",
        f"DELETE FROM link_tokens WHERE user_id IN ({users})",
        f"DELETE FROM feedback WHERE user_id IN ({users})",
        f"DELETE FROM bot_replies WHERE user_id IN ({users})",
        f"DELETE FROM users WHERE {where}",
    ):
        db.exec(text(statement).bindparams(**params))


def cleanup() -> None:
    with session_scope() as db:
        store = db.exec(select(Store).where(Store.slug == SLUG)).first()
        db.exec(text("DELETE FROM feedback WHERE question LIKE :q").bindparams(q=f"%{QUESTION_MARK}%"))
        _delete_users(db, "whatsapp_number = :phone", {"phone": PHONE})
        if store is not None:
            b = {"b": store.store_id}
            for statement in ("DELETE FROM feedback WHERE store_id = :b",
                              "DELETE FROM bot_replies WHERE store_id = :b"):
                db.exec(text(statement).bindparams(**b))
            _delete_users(db, "store_id = :b", b)
            for statement in ("DELETE FROM product_variants WHERE store_id = :b",
                              "DELETE FROM products WHERE store_id = :b",
                              "DELETE FROM processed_messages WHERE store_id = :b",
                              "DELETE FROM stores WHERE store_id = :b"):
                db.exec(text(statement).bindparams(**b))
    tenancy.forget()
    catalog.invalidate()
    training._rules_cache["at"] = 0.0


def fixture_fetch(pages_seen: list, refuse_first: bool = True):
    """Shopify's /products.json from the fixture, one 429 first, honouring limit/page."""
    state = {"refused": not refuse_first}

    def fetch(url: str, params: dict) -> httpx.Response:
        request = httpx.Request("GET", url, params=params)
        if not state["refused"]:
            state["refused"] = True
            return httpx.Response(429, headers={"Retry-After": "3"}, request=request)
        pages_seen.append(params["page"])
        start = (params["page"] - 1) * params["limit"]
        return httpx.Response(200, json={"products": FIXTURE["products"][start:start + params["limit"]]},
                              request=request)
    return fetch


def make_store_b() -> int:
    with session_scope() as db:
        store = Store(slug=SLUG, name="Zorvo Fit (test)", platform="shopify", status="live", code_prefix="TB",
                      connector={"shop_domain": "zorvo-test.myshopify.com"}, wa_phone_number_id=PNID_B,
                      wa_display_number="910000000099", wa_access_token_enc=secrets_box.encrypt(TOKEN_B))
        db.add(store)
        db.flush()
        store_id = store.store_id
    tenancy.forget()
    return store_id


def store_dict(store_id: int) -> dict:
    return tenancy.get_store(store_id)


def main() -> int:
    if not os.environ.get("MUNIM_SECRET_KEYS"):
        os.environ["MUNIM_SECRET_KEYS"] = Fernet.generate_key().decode()
    init_db()
    cleanup()
    try:
        b = make_store_b()

        print("\n1. Shopify importer (fixture, 2 products a page, one 429 first)")
        pages, sleeps = [], []
        shopify_public.PAGE_SIZE, real_page_size = 2, shopify_public.PAGE_SIZE
        try:
            connector = shopify_public.ShopifyPublicConnector(store_dict(b), fetch=fixture_fetch(pages),
                                                              sleep=sleeps.append)
            counts = connector.sync_catalog()
        finally:
            shopify_public.PAGE_SIZE = real_page_size
        check("every page fetched, stopping at the short one", pages == [1, 2, 3], pages)
        check("the 429 was retried after its Retry-After", 3.0 in sleeps, sleeps)
        check("at most one request per second", all(s >= 1.0 for s in sleeps), sleeps)
        with tenancy.use_store(b):
            products = catalog.get_all()
            by_name = {p["name"]: p for p in products}
        check("one product per colour: 5 Shopify products -> 7 here", len(products) == 7 and counts["added"] == 7,
              sorted(by_name))
        check("codes come from the store's prefix", sorted(p["id"] for p in products) ==
              [f"TB{n:03d}" for n in range(1, 8)], [p["id"] for p in products])
        black, white = by_name.get("Oversized Graphic Tee - Black"), by_name.get("Oversized Graphic Tee - White")
        check("each colour has its own photo", black and white and black["image_url"].endswith("ogt-black.jpg")
              and white["image_url"].endswith("ogt-white.jpg"), (black or {}).get("image_url"))
        check("sizes are the variants", black and black["sizes_available"] == ["S", "M", "L", "XL"])
        check("a sold-out size is kept but not in stock", white and white["stock"]["XL"] == 0
              and white["stock"]["M"] == 1, (white or {}).get("stock"))
        check("price and MRP from the variants", black and black["price"] == 699 and black["mrp"] == 999)
        navy = by_name.get("Dri-Fit Training Tee - Navy")
        check("size-then-colour option order is understood", navy and navy["color"] == "Navy"
              and navy["sizes_available"] == ["M", "L"], navy and (navy["color"], navy["sizes_available"]))
        check("gender read from tags", navy and navy["gender"] == "Men"
              and by_name["Seamless Sports Bra"]["gender"] == "Women" and black["gender"] == "")
        check("one colour only: no colour in the name", "Tapered Joggers" in by_name, sorted(by_name))
        check("'Default Title' becomes a one-size product", by_name["Canvas Gym Tote"]["sizes_available"] == ["Free Size"])
        check("HTML is stripped from descriptions", "<" not in black["description"]
              and "240 GSM" in black["description"], black["description"])
        with session_scope() as db:
            variant = db.exec(select(ProductVariant).where(ProductVariant.store_id == b,
                                                           ProductVariant.sku == "ZF-OGT-BLK-M")).first()
            vectors = db.exec(select(Product.embedding_model).where(Product.store_id == b)).all()
            variant = {"id": variant.variant_id, "external_id": variant.external_id} if variant else {}
        check("Shopify's variant id is kept for checkout (P3)", variant.get("external_id") == "900102", variant)
        check("every product is embedded", len(vectors) == 7 and all(vectors), vectors)

        print("\n2. Re-import: same codes, removed products deactivated, prices updated")
        changed = json.loads(json.dumps(FIXTURE))
        changed["products"] = [p for p in changed["products"] if p["id"] != 7005]
        for shopify_variant in changed["products"][0]["variants"]:
            shopify_variant["price"] = "649.00"
        original = FIXTURE["products"]
        FIXTURE["products"] = changed["products"]
        try:
            connector = shopify_public.ShopifyPublicConnector(store_dict(b), fetch=fixture_fetch([], False),
                                                              sleep=lambda s: None)
            counts = connector.sync_catalog()
        finally:
            FIXTURE["products"] = original
        with tenancy.use_store(b):
            again = {p["name"]: p for p in catalog.get_all()}
        check("existing products keep their codes", again["Oversized Graphic Tee - Black"]["id"] == black["id"])
        check("a product the shop dropped is deactivated", "Canvas Gym Tote" not in again
              and counts["deactivated"] == 1, counts)
        check("a new price is picked up", again["Oversized Graphic Tee - Black"]["price"] == 649)

        print("\n3. Catalogues, vectors and search are per store")
        demo_codes = {p["id"] for p in catalog.get_all()}
        check("the demo catalogue has no store B products", not any(c.startswith("TB") for c in demo_codes))
        with tenancy.use_store(b):
            check("store B cannot look up a demo product", catalog.get_by_id("EW001") is None)
            tee = retrieval.search("black oversized t-shirt").products
            check("store B finds its own tee", tee and tee[0]["id"] == black["id"], [p["id"] for p in tee])
            check("store B search never returns a demo product",
                  all(p["id"].startswith("TB") for p in retrieval.search("sherwani for my wedding", k=10).products))
            vector_codes = [code for code, _ in catalog.ranked_by_vector(catalog.stored_vector(black["id"]))]
            check("store B's vector ranking holds only its own products",
                  vector_codes and all(c.startswith("TB") for c in vector_codes), vector_codes)
            similar = [p["id"] for p in search.similar_products(black["id"])]
            check("similar products stay in store B", similar and all(c.startswith("TB") for c in similar), similar)
            check("a code is found only in its own store", find_product_code(f"send me {black['id']}") == black["id"])
        check("store B's code means nothing in the demo store", find_product_code(f"send me {black['id']}") is None)
        check("demo search never returns store B products",
              all(p["id"].startswith("EW") for p in retrieval.search("black oversized t-shirt", k=10).products))

        print("\n4. Customers, carts and orders are per store")
        demo_user = repo.get_or_create_user(PHONE, "Ravi")
        with tenancy.use_store(b):
            b_user = repo.get_or_create_user(PHONE, "Ravi")
            check("one phone number is two customers", b_user != demo_user, (demo_user, b_user))
            cart = repo.add_to_cart(b_user, black["id"], "M")
            line = cart["items"][0]
            check("store B's cart holds its product, with its variant", line["product_id"] == black["id"]
                  and line["variant_id"] == variant["id"], line)
            try:
                repo.add_to_cart(b_user, "EW001", "M")
                check("a demo product cannot go in store B's cart", False)
            except ValueError:
                check("a demo product cannot go in store B's cart", True)
            order = repo.create_order_from_cart(b_user, "bot")
        check("the demo customer's cart is untouched", repo.get_cart(demo_user)["count"] == 0)
        with session_scope() as db:
            row = db.get(Order, order["order_id"])
            check("the order belongs to store B", row.store_id == b, row.store_id)
        check("the demo customer does not see store B's order", repo.get_order_history(demo_user) == [])
        check("order lines carry the variant", repo.get_order(order["order_id"])["items"][0]["product_id"] == black["id"])

        print("\n5. Training is per store")
        training.rate("owner", -1, f"{QUESTION_MARK} oversized tee", "EW001", None, None)
        with tenancy.use_store(b):
            training.rate("owner", -1, f"{QUESTION_MARK} oversized tee", black["id"], None, None)
            b_rules = {r["product_id"] for r in training.learned_rules()}
            check("store B sees only its own rating", b_rules == {black["id"]}, b_rules)
            check("store B's ranking follows its own rating only",
                  training.ranking_adjustments(f"{QUESTION_MARK} oversized tee") == {black["id"]: -1})
            training.rate("customer", -1, "tee", similar[0], None, b_user)
            check("store B's customer hid the item for themselves", similar[0] in training.hidden_for(b_user))
        demo_rules = {r["product_id"] for r in training.learned_rules()}
        check("the demo store does not see store B's rating", black["id"] not in demo_rules)
        check("the demo customer's hidden items are untouched", training.hidden_for(demo_user) == set())

        print("\n6. The webhook answers as the store the message was sent to")
        def payload(body: str, pnid: str | None, phone: str = PHONE) -> dict:
            value = {"messaging_product": "whatsapp",
                     "contacts": [{"wa_id": phone, "profile": {"name": "Ravi"}}],
                     "messages": [{"from": phone, "id": "wamid.tenancy", "timestamp": "1", "type": "text",
                                   "text": {"body": body}}]}
            if pnid:
                value["metadata"] = {"display_phone_number": "x", "phone_number_id": pnid}
            return {"object": "whatsapp_business_account",
                    "entry": [{"id": "1", "changes": [{"field": "messages", "value": value}]}]}
        with TestClient(app) as web:
            local = {"X-Local-Test": "true"}
            reply = web.post("/webhook", json=payload("black oversized t-shirt", PNID_B), headers=local).json()
            photos = reply["responses"][0]["photos"] if reply["responses"] else []
            check("a message to store B's number is answered from store B", photos and
                  all(c.startswith("TB") for c in photos), reply)
            reply = web.post("/webhook", json=payload("hi", "TEST-PNID-NOBODY", "910000000078"),
                             headers=local).json()
            check("a number no store owns is ignored, still with a 200", reply["responses"] == [], reply)
            with session_scope() as db:
                stray = db.exec(select(User).where(User.whatsapp_number == "910000000078")).first()
            check("...and creates no customer anywhere", stray is None)
            reply = web.post("/webhook", json=payload("hi", None), headers=local).json()
            check("no metadata (the simulator) means the demo store", reply["responses"] != [], reply)

        print("\n7. Linking codes do not cross stores")
        demo_web = repo.create_link_token(demo_user)
        with tenancy.use_store(b):
            check("a demo account's code sent to store B's number is refused",
                  repo.consume_link_token(demo_web, PHONE) is None)

        print("\n8. Secrets and WhatsApp credentials")
        with session_scope() as db:
            raw = db.get(Store, b).wa_access_token_enc
        check("the token is not stored in plain text", raw and TOKEN_B not in raw)
        check("store B sends with its own token", tenancy.whatsapp_credentials(b)["access_token"] == TOKEN_B)
        check("...from its own number", tenancy.whatsapp_credentials(b)["phone_number_id"] == PNID_B)
        with session_scope() as db:
            db.get(Store, b).wa_access_token_enc = None
        tenancy.forget()
        try:
            tenancy.whatsapp_credentials(b)
            check("a store without credentials never borrows the demo's", False)
        except RuntimeError:
            check("a store without credentials never borrows the demo's", True)
        old_keys = os.environ["MUNIM_SECRET_KEYS"]
        secret = secrets_box.encrypt("rotate me")
        new_key = Fernet.generate_key().decode()
        os.environ["MUNIM_SECRET_KEYS"] = f"{new_key},{old_keys}"
        rotated = secrets_box.rotate(secret)
        os.environ["MUNIM_SECRET_KEYS"] = new_key
        check("after rotation the new key alone decrypts", secrets_box.decrypt(rotated) == "rotate me")
        try:
            secrets_box.decrypt(secret)
            check("...and the old ciphertext no longer does", False)
        except secrets_box.SecretsUnavailable:
            check("...and the old ciphertext no longer does", True)
        os.environ["MUNIM_SECRET_KEYS"] = old_keys

        print("\n9. Meta redelivery is caught in the database")
        check("first delivery is claimed", repo.claim_message("wamid.tenancy-once", b))
        check("the redelivery is not", not repo.claim_message("wamid.tenancy-once", b))
    finally:
        cleanup()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
