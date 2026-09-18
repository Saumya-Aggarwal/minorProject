"""Cart and order checks against the real database.

Creates its own throwaway users and deletes only those, so running it does not
disturb real accounts (unlike test_linking.py, which wipes everything).

Run from the repo root:  backend/.venv/Scripts/python scripts/test_cart.py
"""

import sys
import threading
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from sqlmodel import select  # noqa: E402

import repository as repo  # noqa: E402
from auth import create_web_user  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

# Recognisable test identities, cleaned up before and after
TEST_PHONES = ["910000000001", "910000000002", "910000000003"]
TEST_EMAILS = ["test-cart-a@example.com", "test-cart-b@example.com"]

MULTI_SIZE = "EW001"   # Brocade Silk Kurta, S-XXL, Rs 2499
SINGLE_SIZE = "EW012"  # Cream Silk Stole, Free Size only, Rs 899
OTHER = "EW003"        # Pastel Chikankari Kurta

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


def raises(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(
            select(User).where(
                (User.whatsapp_number.in_(TEST_PHONES)) | (User.email.in_(TEST_EMAILS))
            )
        ).all()
        ids = [user.user_id for user in users]
        if not ids:
            return
        order_ids = [o.order_id for o in db.exec(select(Order).where(Order.user_id.in_(ids))).all()]
        for model, column, values in (
            (OrderItem, OrderItem.order_id, order_ids),
            (Order, Order.user_id, ids),
            (CartItem, CartItem.user_id, ids),
            (Session, Session.user_id, ids),
            (LinkToken, LinkToken.user_id, ids),
        ):
            if values:
                for row in db.exec(select(model).where(column.in_(values))).all():
                    db.delete(row)
        db.flush()
        for user in users:
            db.delete(user)


def cart_rows(user_id: int) -> list[tuple[str, str, int]]:
    return [(i["product_id"], i["size"], i["quantity"]) for i in repo.get_cart(user_id)["items"]]


def main() -> int:
    init_db()
    cleanup()

    print("\n1. Adding items")
    user = repo.get_or_create_user(TEST_PHONES[0], "Cart Tester")
    repo.add_to_cart(user, MULTI_SIZE, "M")
    cart = repo.add_to_cart(user, MULTI_SIZE, "M")
    check("same item twice becomes one line", cart_rows(user) == [(MULTI_SIZE, "M", 2)],
          f"got {cart_rows(user)}")
    check("count sums quantities", cart["count"] == 2)
    check("total uses catalogue price", cart["total_inr"] == 2 * 2499, f"got {cart['total_inr']}")

    repo.add_to_cart(user, SINGLE_SIZE)
    stole = [i for i in repo.get_cart(user)["items"] if i["product_id"] == SINGLE_SIZE][0]
    check("single-size product picks its only size", stole["size"] == "Free Size",
          f"got {stole['size']!r}")

    repo.add_to_cart(user, OTHER)
    repo.add_to_cart(user, OTHER)
    unsized = [i for i in repo.get_cart(user)["items"] if i["product_id"] == OTHER]
    check("no size given twice is still one line (NOT NULL size)",
          len(unsized) == 1 and unsized[0]["quantity"] == 2, f"got {unsized}")

    check("invalid size rejected", raises(repo.add_to_cart, user, MULTI_SIZE, "XXXL"))
    check("unknown product rejected", raises(repo.add_to_cart, user, "EW999"))
    check("zero quantity rejected", raises(repo.add_to_cart, user, MULTI_SIZE, "M", 0))

    print("\n2. Concurrent adds (atomic upsert)")
    racer = repo.get_or_create_user(TEST_PHONES[1], "Racer")
    errors: list[Exception] = []

    def add_once() -> None:
        try:
            repo.add_to_cart(racer, SINGLE_SIZE)
        except Exception as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=add_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("8 simultaneous adds raise nothing", not errors, f"errors: {errors[:1]}")
    check("8 simultaneous adds give quantity 8", cart_rows(racer) == [(SINGLE_SIZE, "Free Size", 8)],
          f"got {cart_rows(racer)}")

    print("\n3. Editing lines")
    line = [i for i in repo.get_cart(user)["items"] if i["product_id"] == MULTI_SIZE][0]
    check("update quantity", repo.update_cart_item(user, line["cart_item_id"], 5))
    check("quantity is now 5", [i["quantity"] for i in repo.get_cart(user)["items"]
                                if i["product_id"] == MULTI_SIZE] == [5])
    check("another user cannot edit it", not repo.update_cart_item(racer, line["cart_item_id"], 1))

    other_line = [i for i in repo.get_cart(user)["items"] if i["product_id"] == OTHER][0]
    check("choose a size on an unsized line",
          repo.update_cart_item_size(user, other_line["cart_item_id"], "L"))
    repo.add_to_cart(user, OTHER)  # a fresh unsized line
    fresh = [i for i in repo.get_cart(user)["items"] if i["product_id"] == OTHER and i["size"] == ""][0]
    repo.update_cart_item_size(user, fresh["cart_item_id"], "L")
    merged = [i for i in repo.get_cart(user)["items"] if i["product_id"] == OTHER]
    check("choosing a size already in the cart merges lines",
          len(merged) == 1 and merged[0]["size"] == "L" and merged[0]["quantity"] == 3,
          f"got {[(i['size'], i['quantity']) for i in merged]}")

    check("the merged-away row no longer exists",
          not repo.update_cart_item(user, fresh["cart_item_id"], 1))
    check("quantity 0 removes the line", repo.update_cart_item(user, merged[0]["cart_item_id"], 0))
    check("removed line is gone", all(i["product_id"] != OTHER for i in repo.get_cart(user)["items"]))

    print("\n4. Checkout")
    # Cart now: EW001 M x5 (2499) + EW012 Free Size x1 (899)
    placed = repo.create_order_from_cart(user, "bot")
    expected = 5 * 2499 + 899
    check("order created from cart", placed is not None)
    check("order total", placed and placed["total_inr"] == expected, f"got {placed}")

    history = repo.get_order_history(user)
    check("history has one order with two lines",
          len(history) == 1 and len(history[0]["items"]) == 2, f"got {history}")
    with session_scope() as db:
        stored = db.get(Order, placed["order_id"]).total_inr
    check("stored total is exact NUMERIC", stored == Decimal(expected), f"got {stored!r}")
    check("cart kept until payment is captured", len(repo.get_cart(user)["items"]) == 2)

    empty_user = repo.get_or_create_user(TEST_PHONES[2], "Empty")
    check("empty cart gives no order", repo.create_order_from_cart(empty_user, "bot") is None)

    print("\n5. Purchase history contract (C2)")
    repo.create_order_for_product(user, OTHER, "M", 1, "web")
    check("single-item order", len(repo.get_order_history(user)) == 2)
    ids = repo.get_purchased_product_ids(user)
    check("most recent first, distinct", ids[0] == OTHER and len(ids) == len(set(ids)),
          f"got {ids}")
    with session_scope() as db:
        latest = db.exec(select(Order).where(Order.user_id == user)
                         .order_by(Order.order_id.desc())).first()
        latest.status = "failed"
    check("failed orders excluded", OTHER not in repo.get_purchased_product_ids(user),
          f"got {repo.get_purchased_product_ids(user)}")

    print("\n6. Cart follows the customer through account linking")
    bot_user = repo.get_or_create_user(TEST_PHONES[2], "Linker")  # reuses the empty user
    repo.add_to_cart(bot_user, MULTI_SIZE, "L")
    repo.add_to_cart(bot_user, OTHER, "S")
    web_user = create_web_user(TEST_EMAILS[0], "test-password-123", "Web Linker")
    repo.add_to_cart(web_user, MULTI_SIZE, "L")
    repo.add_to_cart(web_user, MULTI_SIZE, "L")
    token = repo.create_link_token(web_user)
    linked = repo.consume_link_token(token, TEST_PHONES[2])
    check("link succeeds with overlapping carts", linked is not None and linked["user_id"] == web_user)
    rows = sorted(cart_rows(web_user))
    check("same item merged, quantities added", (MULTI_SIZE, "L", 3) in rows, f"got {rows}")
    check("other item moved across", (OTHER, "S", 1) in rows, f"got {rows}")
    with session_scope() as db:
        orphan_left = db.exec(select(CartItem).where(CartItem.user_id == bot_user)).all()
    check("nothing left on the deleted bot user", not orphan_left)

    print("\n7. Cart by WhatsApp, through the real webhook")
    bot_conversation_checks()

    print(f"\n{passed} passed, {failed} failed")
    cleanup()
    return 1 if failed else 0


def bot_conversation_checks() -> None:
    from fastapi.testclient import TestClient

    from main import app

    phone = TEST_PHONES[0]

    def say(client: TestClient, body: str) -> str:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": phone, "profile": {"name": "Cart Tester"}}],
                "messages": [{"from": phone, "id": "wamid.t", "timestamp": "1",
                              "type": "text", "text": {"body": body}}],
            }}]}],
        }
        response = client.post("/webhook", json=payload, headers={"X-Local-Test": "true"})
        response.raise_for_status()
        return response.json()["responses"][0]["body"]

    cleanup()
    user = repo.get_or_create_user(phone, "Cart Tester")

    with TestClient(app) as client:
        check("CART on a fresh chat says empty", "cart is empty" in say(client, "CART").lower())
        check("ADD with nothing selected asks to pick", "pick an item" in say(client, "ADD").lower())

        results = say(client, "sherwani for my wedding")
        check("search results carry the C1 footer", "CART to see your cart" in results)

        chosen = say(client, "1")
        check("choosing a multi-size item lists sizes", "Sizes:" in chosen, chosen[:80])

        check("unsized BUY asks which size", "which size" in say(client, "BUY").lower())

        added = say(client, "ADD")
        check("ADD without a size still adds", "Added" in added and "size not chosen" in added, added[:90])

        blocked = say(client, "CHECKOUT")
        check("CHECKOUT refuses unsized lines", "Choose a size" in blocked, blocked[:80])
        check("no order created while sizes are missing", not repo.get_order_history(user))

        sizes = repo.get_cart(user)["items"][0]["sizes_available"]
        sized = say(client, f"SIZE 1 {sizes[0]}")
        check("SIZE sets the size", f"set to size {sizes[0]}" in sized, sized[:80])

        check("SIZE with an invalid size is refused", "comes in" in say(client, "SIZE 1 XXXL"))
        check("REMOVE out of range is explained", "no item 9" in say(client, "REMOVE 9").lower())

        say(client, "light kurti for office")
        say(client, "2")
        say(client, "ADD")
        cart = repo.get_cart(user)
        check("second item added from a new search", len(cart["items"]) == 2)

        # Give any unsized line a size, then check out
        for position, item in enumerate(cart["items"], start=1):
            if not item["size"] and len(item["sizes_available"]) > 1:
                say(client, f"SIZE {position} {item['sizes_available'][0]}")

        done = say(client, "CHECKOUT")
        check("CHECKOUT creates one order", "created" in done and len(repo.get_order_history(user)) == 1,
              done[:80])
        order = repo.get_order_history(user)[0]
        check("order has both lines, all sized",
              len(order["items"]) == 2 and all(i["size"] for i in order["items"]),
              f"got {order['items']}")
        check("order total matches the cart", order["total_inr"] == repo.get_cart(user)["total_inr"])

        say(client, "REMOVE 1")
        check("REMOVE drops a line", len(repo.get_cart(user)["items"]) == 1)
        check("CLEAR empties the cart", "empty" in say(client, "clear cart").lower()
              and not repo.get_cart(user)["items"])

        check("a normal search is not mistaken for a command",
              "CART to see your cart" in say(client, "add a red dupatta"))


if __name__ == "__main__":
    sys.exit(main())
