"""End-to-end check of the account-linking flow.

Covers the case that matters most on demo day: the customer messaged the bot
before linking, so a bot-created user row already owns their phone number and
has to be folded into the website account.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_linking.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from fastapi.testclient import TestClient  # noqa: E402

import repository as repo  # noqa: E402
from db import session_scope  # noqa: E402
from main import app  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

PHONE = "919876543210"
EMAIL = "demo@example.com"
PASSWORD = "demo-password-123"

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


def wipe() -> None:
    with session_scope() as db:
        # Children before parents: order_items -> orders -> users
        for model in (LinkToken, OrderItem, Order, CartItem, Session, User):
            for row in db.exec(__import__("sqlmodel").select(model)).all():
                db.delete(row)


def whatsapp_message(client: TestClient, body: str) -> str:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": PHONE, "profile": {"name": "Phone Contact"}}],
            "messages": [{"from": PHONE, "id": "wamid.x", "timestamp": "1",
                          "type": "text", "text": {"body": body}}],
        }}]}],
    }
    response = client.post("/webhook", json=payload, headers={"X-Local-Test": "true"})
    response.raise_for_status()
    return response.json()["responses"][0]["body"]


def main() -> int:
    wipe()
    client = TestClient(app)

    with client:
        print("\n1. Bot contact before linking (creates the orphan row)")
        whatsapp_message(client, "do you have silk kurtas?")
        orphan = repo.get_user_by_whatsapp(PHONE)
        check("bot-created user exists", orphan is not None)
        check("not yet linked", orphan is not None and not orphan["is_linked"])
        orphan_id = orphan["user_id"]

        print("\n2. Website signup and order")
        client.post("/signup", data={"email": EMAIL, "password": PASSWORD,
                                     "display_name": "Demo User"}, follow_redirects=False)
        me = client.get("/api/me")
        check("signed up and session set", me.status_code == 200)
        web_id = me.json()["user_id"]
        check("website account is a separate row", web_id != orphan_id,
              f"(web={web_id} bot={orphan_id})")

        client.post("/buy/EW001", data={"size": "M"}, follow_redirects=False)
        check("web order recorded", len(repo.get_order_history(web_id)) == 1)

        print("\n3. Password storage")
        with session_scope() as db:
            from sqlmodel import select
            user = db.exec(select(User).where(User.email == EMAIL)).first()
            stored = user.password_hash
        check("password is hashed, not plaintext", PASSWORD not in (stored or ""))
        check("bcrypt format", (stored or "").startswith("$2b$"))

        print("\n4. Link token issued")
        started = client.post("/api/link/start")
        check("token minted", started.status_code == 200)
        code = started.json()["message"]
        check("wa.me link built", started.json()["whatsapp_url"].startswith("https://wa.me/"))

        print("\n5. Sending the code from WhatsApp (the merge)")
        reply = whatsapp_message(client, code)
        check("greets by name", "Demo User" in reply, f"got: {reply[:70]}")
        check("mentions prior purchase", "Brocade" in reply, f"got: {reply[:70]}")

        linked = repo.get_user_by_whatsapp(PHONE)
        check("number now on the website account", linked["user_id"] == web_id)
        check("account reports linked", linked["is_linked"])

        with session_scope() as db:
            from sqlmodel import select
            remaining = db.exec(select(User)).all()
            orphan_gone = db.get(User, orphan_id) is None
        check("orphan row merged away", orphan_gone)
        check("exactly one user row", len(remaining) == 1, f"(found {len(remaining)})")

        print("\n6. Token safety")
        check("replayed code refused", "invalid or has expired" in whatsapp_message(client, code))
        check("garbage code refused", "invalid or has expired" in
              whatsapp_message(client, "LINK-nonsense"))

        print("\n7. Normal message after linking")
        whatsapp_message(client, "something for a wedding")
        check("history visible to the bot", len(repo.get_order_history(web_id)) == 1)

    print(f"\n{passed} passed, {failed} failed")
    wipe()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
