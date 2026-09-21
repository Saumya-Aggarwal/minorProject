"""Storefront checks: listing filters and sort, search, similar products,
address validation, safe sign-in redirects, 404s, and product codes arriving
from the website's "Ask about this on WhatsApp" button.

Razorpay and WhatsApp are faked. Creates and removes only its own users.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_store.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import catalog  # noqa: E402
import checkout  # noqa: E402
import payments  # noqa: E402
import repository as repo  # noqa: E402
import search  # noqa: E402
import shipping  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

PHONE = "910000000031"
EMAIL = "test-store@example.com"

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


async def fake_link(order_id, amount_inr, description, **_):
    return {"id": f"plink_store{order_id}", "short_url": f"https://rzp.io/test/store{order_id}"}


async def no_whatsapp(to, body):
    return {}


payments.create_payment_link = fake_link
checkout.send_whatsapp_message = no_whatsapp
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
# Deterministic and free: no LLM calls; the assistant has its own test with a fake model
os.environ["LLM_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(select(User).where((User.whatsapp_number == PHONE) | (User.email == EMAIL))).all()
        ids = [u.user_id for u in users]
        if not ids:
            return
        order_ids = [o.order_id for o in db.exec(select(Order).where(Order.user_id.in_(ids))).all()]
        if order_ids:
            for row in db.exec(select(OrderItem).where(OrderItem.order_id.in_(order_ids))).all():
                db.delete(row)
        for model, column in ((Order, Order.user_id), (CartItem, CartItem.user_id),
                              (Session, Session.user_id), (LinkToken, LinkToken.user_id)):
            for row in db.exec(select(model).where(column.in_(ids))).all():
                db.delete(row)
        db.flush()
        for user in users:
            db.delete(user)


def names(html: str, products: list[dict]) -> list[str]:
    return [p["name"] for p in products if p["name"] in html]


def main() -> int:
    init_db()
    cleanup()
    everything = catalog.get_all()

    print("\n1. Filtering and sorting (catalog.py)")
    sarees = catalog.filter_products(everything, gender="Women", category="Saree")
    check("women's sarees", sarees and all(p["category"] == "Saree" and p["gender"] == "Women" for p in sarees))
    band = catalog.filter_products(everything, price="1500-3000")
    check("price band is inclusive", band and all(1500 <= p["price"] <= 3000 for p in band))
    wedding = catalog.filter_products(everything, occasion="wedding")
    check("occasion group matches any of its occasions",
          wedding and all(set(p["occasion"]) & catalog.OCCASION_GROUPS["wedding"][1] for p in wedding))
    prices = [p["price"] for p in catalog.sort_products(everything, "price-asc")]
    check("price low to high", prices == sorted(prices))
    facets = catalog.facets(everything, "Women", "", "", "")
    check("gender facet ignores its own filter", dict((v, c) for v, _, c in facets["gender"]) == {"Men": 16, "Women": 16})
    check("category facet respects the gender filter",
          all(label in {p["category"] for p in everything if p["gender"] == "Women"} for _, label, _ in facets["category"]))

    with TestClient(app) as web:
        print("\n2. Listing pages")
        page = web.get("/shop?gender=Women&category=Saree&sort=price-desc")
        found = names(page.text, sarees)
        check("/shop shows exactly the filtered products", sorted(found) == sorted(p["name"] for p in sarees))
        order = [page.text.index(p["name"]) for p in catalog.sort_products(sarees, "price-desc")]
        check("/shop respects the sort", order == sorted(order))
        check("unknown filter values are ignored, not errors",
              web.get("/shop?gender=Robots&category=<script>&price=free").status_code == 200)
        check("filter chips can be removed", "Clear all" in page.text)

        print("\n3. Search (same engine as the bot)")
        results = search.search_products("silk saree for a wedding")
        check("search finds a saree for 'silk saree for a wedding'",
              any(p["category"] == "Saree" for p in results[:3]), [p["name"] for p in results[:3]])
        check("nonsense finds nothing", search.search_products("zzqqxx vbnm") == [])
        page = web.get("/search?q=silk+saree+for+a+wedding")
        check("search page renders results", page.status_code == 200 and "Results for" in page.text
              and any(p["name"] in page.text for p in results[:3]))
        check("empty search is a friendly page", "What are you looking for" in web.get("/search?q=").text)

        print("\n4. Similar products")
        similar = search.similar_products("EW006")
        check("similar excludes the product itself", similar and all(p["id"] != "EW006" for p in similar))
        check("similar stays within the same gender", all(p["gender"] == "Men" for p in similar))
        page = web.get("/product/EW006")
        check("product page shows 'You may also like'", "You may also like" in page.text
              and any(p["name"] in page.text for p in similar))
        check("product page has the WhatsApp question pre-filled with the code",
              "EW006" in page.text and "wa.me" in page.text)

        print("\n5. Pages that should not exist")
        check("unknown product is a branded 404", web.get("/product/EW999").status_code == 404
              and "This page has moved on" in web.get("/product/EW999").text)
        check("unknown page is a branded 404", "This page has moved on" in web.get("/definitely-not-here").text)
        check("API 404s stay JSON", web.get("/api/nope").headers["content-type"].startswith("application/json"))

        print("\n6. Sign-in redirects")
        to_login = web.get("/checkout", follow_redirects=False).headers.get("location", "")
        check("signed-out checkout goes to sign-in with a way back", to_login.startswith("/login?next=%2Fcheckout"))
        web.post("/signup", data={"email": EMAIL, "password": "test-password-123",
                                  "display_name": "Store Tester", "next": "https://evil.example/steal"},
                 follow_redirects=False)
        check("an off-site 'next' is ignored after sign-up",
              web.get("/login?next=https://evil.example", follow_redirects=False).headers.get("location") == "/account")
        check("'//evil' is not treated as same-site",
              web.get("/login?next=//evil.example", follow_redirects=False).headers.get("location") == "/account")
        check("a same-site 'next' is honoured",
              web.get("/login?next=/cart", follow_redirects=False).headers.get("location") == "/cart")
        user_id = web.get("/api/me").json()["user_id"]

        print("\n7. Address validation (shipping.py)")
        good, errors = shipping.validate({"name": "Asha Rao", "phone": "+91 98765 43210", "line1": "Flat 4B, Lake View",
                                          "city": "Bengaluru", "state": "Karnataka", "pincode": "560034"})
        check("valid address passes", not errors, errors)
        check("phone normalised to 10 digits", good["phone"] == "9876543210")
        _, errors = shipping.validate({"name": "A", "phone": "12345", "line1": "x", "city": "",
                                       "state": "Atlantis", "pincode": "012345"})
        check("every bad field is reported", set(errors) == {"name", "phone", "line1", "city", "state", "pincode"})
        check("PIN codes cannot start with 0", "pincode" in errors)
        check("API checkout rejects a bad address with field errors",
              web.post("/api/checkout", json={"pincode": "12"}).status_code == 422)

        print("\n8. Chat orders can get an address afterwards")
        bot_order = repo.create_order_for_product(user_id, "EW012", "", 1, "bot")
        page = web.get(f"/orders/{bot_order}")
        check("order without an address asks for one", "Tell us where to send it" in page.text)
        bad = web.post(f"/orders/{bot_order}/address", data={"pincode": "1"}, follow_redirects=False)
        check("invalid address is refused on the order page", bad.status_code == 400)
        good_post = web.post(f"/orders/{bot_order}/address",
                             data={"name": "Store Tester", "phone": "9876543210", "line1": "22 Ring Road",
                                   "city": "Delhi", "state": "Delhi", "pincode": "110001"},
                             follow_redirects=False)
        check("valid address is saved", good_post.status_code == 303
              and repo.get_order(bot_order)["shipping"]["city"] == "Delhi")
        check("later chat orders reuse it", repo.get_last_shipping(user_id)["pincode"] == "110001")

        print("\n9. 'Ask about this on WhatsApp' lands on that product")
        payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
            "field": "messages", "value": {
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": PHONE, "profile": {"name": "Store Tester"}}],
                "messages": [{"from": PHONE, "id": "wamid.s", "timestamp": "1", "type": "text",
                              "text": {"body": "Tell me about the Royal Blue Wedding Sherwani (EW006)"}}]}}]}]}
        reply = web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]["body"]
        check("bot describes the product from its code", "Royal Blue Wedding Sherwani" in reply and "Sizes" in reply,
              reply[:80])
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "ADD 42"
        added = web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]["body"]
        check("...and it is selected, so ADD 42 works next", "Added Royal Blue Wedding Sherwani" in added, added[:80])

    print(f"\n{passed} passed, {failed} failed")
    cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
