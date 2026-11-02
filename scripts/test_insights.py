"""/admin/insights: the owner's numbers, counted from turn_traces (insights.py).

A customer sends a few messages through the real webhook (a scripted fake
model, no network): a greeting, a command, an AI reply whose made-up price a
check sends back, and a search for something we do not sell. Then:
  * the counts move by exactly what was sent (messages, routes, AI share,
    checks), and the eval harness's own traces are not counted
  * the missed-demand list shows the search that had to be loosened
  * the page renders those numbers for the owner; a customer gets 403 and a
    stranger is sent to sign in

Creates and removes its own users (their traces go with them).
Run from the repo root:  backend/.venv/Scripts/python scripts/test_insights.py
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ["WHATSAPP_APP_SECRET"] = ""
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
os.environ["ADMIN_EMAILS"] = "test-insights-owner@example.com"
os.environ["LLM_API_KEY"] = "fake-key-for-tests"
os.environ["LLM_MODEL"] = "fake-primary"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import insights  # noqa: E402
from bot import assistant  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import CartItem, LinkToken, Session, User  # noqa: E402

PHONE = "910000000071"
OWNER = "test-insights-owner@example.com"
STAFF = "test-insights-staff@example.com"
passed, failed = 0, 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


def calls(*pairs):
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(pairs)])


def script(*steps):
    queue = list(steps)
    return lambda model, messages: queue.pop(0) if queue else SimpleNamespace(content="(ran out)", tool_calls=None)


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(select(User).where((User.whatsapp_number == PHONE) | User.email.in_([OWNER, STAFF]))).all()
        ids = [u.user_id for u in users]
        for model, column in ((CartItem, CartItem.user_id), (Session, Session.user_id),
                              (LinkToken, LinkToken.user_id)):
            for row in db.exec(select(model).where(column.in_(ids))).all():
                db.delete(row)
        db.flush()
        for user in users:
            db.delete(user)       # their turn_traces go with them (CASCADE)


def main() -> int:
    init_db()
    cleanup()
    before = insights.summary(1)

    with TestClient(app) as web:
        def send(body: str) -> str:
            payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                "field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": PHONE, "profile": {"name": "Meera"}}],
                    "messages": [{"from": PHONE, "id": "wamid.t", "timestamp": "1", "type": "text",
                                  "text": {"body": body}}]}}]}]}
            return web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]["body"]

        print("\n1. A few messages, each leaving a trace")
        send("hi")
        send("CART")
        # The model states a price nobody said: the amount check sends it back once
        assistant._complete = script(
            calls(("send_reply", {"message": "Our festive kurtas start at Rs 199, a steal!"})),
            calls(("send_reply", {"message": "Tell me the occasion and I will pick a few."})),
        )
        send("do you have festive kurtas")
        # Purple sherwanis: none, so the search loosens the colour (and says so)
        assistant._complete = script(
            calls(("search_products", {"query": "purple sherwani", "categories": ["Sherwani"], "colour": "purple"})),
            calls(("send_reply", {"message": "No purple ones right now; these are close."})),
        )
        send("purple sherwani for my wedding")

        after = insights.summary(1)
        check("4 more messages counted", after["messages"] - before["messages"] == 4,
              (before["messages"], after["messages"]))
        check("one more customer", after["customers"] - before["customers"] == 1)
        routes = dict(after["routes"])
        check("the greeting and CART are answered by code",
              routes.get("Greeting", 0) - dict(before["routes"]).get("Greeting", 0) == 1
              and routes.get("CART command", 0) - dict(before["routes"]).get("CART command", 0) == 1, routes)
        check("two AI replies", after["ai_turns"] - before["ai_turns"] == 2)
        guards = dict(after["guards"])
        price = insights.GUARDS["amount_retry"]
        check("the made-up price shows as a check that stepped in",
              guards.get(price, 0) - dict(before["guards"]).get(price, 0) == 1, guards)
        missed = {m["wanted"]: m for m in after["missed"]}
        purple = next((m for w, m in missed.items() if "Sherwani" in w and "purple" in w), None)
        check("the purple sherwani is missed demand, with what was loosened",
              purple is not None and "colour, fabric or fit" in purple["how"], missed)
        check("the latest message is first in Recent", after["recent"][0]["question"] == "purple sherwani for my wedding",
              after["recent"][0])
        check("customers' numbers are masked", "••••" in after["recent"][0]["customer"], after["recent"][0])

        print("\n2. The page")
        check("signed out: sent to sign in",
              TestClient(app).get("/admin/insights", follow_redirects=False).headers.get("location", "")
              .startswith("/login"))
        web.post("/signup", data={"email": OWNER, "password": "owner-password-1", "display_name": "Owner"})
        page = web.get("/admin/insights?days=1")
        check("the owner sees it", page.status_code == 200, page.status_code)
        check("with the counts and the check in words",
              f">{after['messages']}<" in page.text and price.split("→")[0].strip() in page.text, page.text[:300])
        check("and the missed demand", "purple" in page.text)
        staff = TestClient(app)
        staff.post("/signup", data={"email": STAFF, "password": "staff-password-1", "display_name": "Staff"})
        check("another signed-in customer gets 403", staff.get("/admin/insights").status_code == 403)

    cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
