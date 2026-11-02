"""Size guide (sizing.py): which size fits, decided by code.

Checked:
  * the customer's numbers are read from their words (chest, bust, waist,
    foot length; inches or cm), and a sizing question is told apart from a
    size being chosen ("my chest is 40, which size?" vs "i think 11 size
    would be the best for me")
  * each kind of product gets its own chart: letters by gender, chest
    numbers, UK shoe sizes by foot length, Free Size; the larger size wins
    between chest and waist, and beyond our largest size we say so
  * in chat, with an item picked, the guide is answered by code and the cart
    is untouched; "ADD 42" then adds the size it named
  * the assistant's get_size_guide tool reads the measurements from the
    customer's words, not the model's, and its chart goes under the reply

A scripted fake model, no network. Creates and removes only its own users.
Run from the repo root:  backend/.venv/Scripts/python scripts/test_sizing.py
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
os.environ["WHATSAPP_APP_SECRET"] = ""   # unsigned test messages
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
os.environ["LLM_API_KEY"] = "fake-key-for-tests"
os.environ["LLM_MODEL"] = "fake-primary"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import catalog  # noqa: E402
import repository as repo  # noqa: E402
import sizing  # noqa: E402
from bot import assistant  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import CartItem, Session, User  # noqa: E402

PHONE = "910000000061"
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


class Script:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def __call__(self, model, messages):
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        return self.steps.pop(0) if self.steps else SimpleNamespace(content="(script ran out)", tool_calls=None)


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(select(User).where(User.whatsapp_number == PHONE)).all()
        ids = [u.user_id for u in users]
        for model, column in ((CartItem, CartItem.user_id), (Session, Session.user_id)):
            for row in db.exec(select(model).where(column.in_(ids))).all():
                db.delete(row)
        db.flush()
        for user in users:
            db.delete(user)


def first(gender: str, category: str) -> dict:
    return next(p for p in catalog.get_all() if p["gender"] == gender and p["category"] == category)


def main() -> int:
    init_db()
    cleanup()

    print("\n1. Reading the customer's numbers")
    m = sizing.measurements
    check("'my chest is 41, which size?' -> chest 41", m("my chest is 41, which size?") == {"chest": 41.0})
    check("'40 inch chest' -> chest 40", m("would it fit a 40 inch chest") == {"chest": 40.0})
    check("'bust 92 cm' -> 36.2 inches", m("bust 92 cm") == {"chest": 36.2})
    check("'waist 33 inches' -> waist 33", m("waist 33 inches") == {"waist": 33.0})
    check("'bust 35 waist 31' -> both", m("bust 35 waist 31") == {"chest": 35.0, "waist": 31.0})
    check("'foot 26.5 cm' -> foot 26.5", m("foot 26.5 cm") == {"foot_cm": 26.5})
    check("no numbers in 'sherwani in 42 for my wedding'", m("sherwani in 42 for my wedding") == {})
    check("the latest number wins ('bust 35 ...' earlier, 'chest 39' now)",
          m("will the lehenga fit me? bust 35 waist 31 what size am i for a kurta, chest 39")["chest"] == 39.0)

    ask = sizing.asks_for_size_help
    for text in ("size guide", "what size am i", "will L fit me?", "my chest is 41, which size should i take?",
                 "chest 41"):
        check(f"{text!r} is a sizing question", ask(text))
    for text in ("i think 11 size would be the best for me", "i think medium would look good on her", "42",
                 "size 42", "xxl", "i will take xl", "sherwani in 42 for my wedding", "under 3000"):
        check(f"{text!r} is not", not ask(text))

    print("\n2. One chart per kind of product")
    sherwani, lehenga = first("Men", "Sherwani"), first("Women", "Lehenga")
    shoes, saree, kurta = first("Men", "Footwear"), first("Women", "Saree"), first("Men", "Kurta")
    check("sherwani, chest 41 -> 42 (between sizes: the larger)",
          sizing.recommend(sherwani, {"chest": 41})[0] == "42")
    check("sherwani, chest 38 -> 38", sizing.recommend(sherwani, {"chest": 38})[0] == "38")
    check("lehenga, bust 35 (M) + waist 31 (L) -> L, the larger",
          sizing.recommend(lehenga, {"chest": 35, "waist": 31})[0] == "L")
    check("mojari, foot 26.5 cm -> UK 9", sizing.recommend(shoes, {"foot_cm": 26.5})[0] == "UK 9")
    check("kurta, chest 39 -> L", sizing.recommend(kurta, {"chest": 39})[0] == "L")
    big = sizing.recommend(kurta, {"chest": 50})
    check("kurta, chest 50 -> beyond our largest, said so", big[0] == "XXL" and big[2] is False, big)
    check("a men's kurta has no waist row", list(sizing.chart(kurta)) == ["chest"])
    check("a lehenga's chart says bust", "Body bust" in sizing.guide(lehenga, "size guide"))
    guide = sizing.guide(kurta, "size guide")
    check("no measurement: the chart and what to send", "Body chest (inches)" in guide and "chest 38" in guide,
          guide)
    check("a saree is one size, nothing to choose", "one size" in sizing.guide(saree, "size guide"))
    check("beyond our largest: not recommended", "too tight" in sizing.guide(kurta, "chest 50"))
    check("a sold-out size is never offered to add",
          "sold out" in sizing.guide(sherwani, "chest 41", in_stock=["38", "40"]))

    with TestClient(app) as web:
        def send(body: str) -> str:
            payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                "field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": PHONE, "profile": {"name": "Ravi"}}],
                    "messages": [{"from": PHONE, "id": "wamid.t", "timestamp": "1", "type": "text",
                                  "text": {"body": body}}]}}]}]}
            return web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]["body"]

        def cart_lines() -> list:
            user = repo.get_user_by_whatsapp(PHONE)
            return repo.get_cart(user["user_id"])["items"] if user else []

        print("\n3. In chat, with an item picked: answered by code")
        script = Script()
        assistant._complete = script
        send(sherwani["id"])
        reply = send("my chest is 41, which size should i take?")
        check("the guide names 42 for a 41 chest", "*42* is your size" in reply, reply)
        check("the model was not asked", script.seen == [])
        check("nothing was added to the cart", cart_lines() == [], cart_lines())
        reply = send("ADD 42")
        lines = cart_lines()
        check("ADD 42 then adds the size the guide named",
              len(lines) == 1 and lines[0]["size"] == "42" and lines[0]["product_id"] == sherwani["id"], lines)
        reply = send("size guide")
        check("'size guide' alone shows the chart and asks for a measurement",
              "Size guide" in reply and "Send your chest" in reply, reply)

        print("\n4. The assistant's tool: numbers from the customer, chart from code")
        cleanup()
        script = Script(
            calls(("get_size_guide", {"product_id": lehenga["id"]})),
            calls(("send_reply", {"message": "Here is the fit for that lehenga."})),
        )
        assistant._complete = script
        reply = send(f"will the {lehenga['name'].lower()} fit me? bust 35 waist 31")
        check("the model's sentence comes first", reply.startswith("Here is the fit"), reply)
        check("the chart and the size are under it, from code", "*L* is your size" in reply, reply)
        tool_result = next((m["content"] for m in script.seen[-1] if m.get("role") == "tool"), "")
        check("the tool read the measurements from the customer's words", "waist 31 in → L" in tool_result,
              tool_result)

        script = Script(
            calls(("get_size_guide", {"category": "Kurta", "gender": "Men"})),
            calls(("send_reply", {"message": "For our men's kurtas:"})),
        )
        assistant._complete = script
        reply = send("what size am i for a kurta, chest 39")
        check("by category, with no product in question", "*L* is your size" in reply, reply)

        # Live, 2 Nov: a sherwani was picked, the model copied "L is your size" for
        # a kurta, and the size check sent it back as a size the sherwani lacks
        send(sherwani["id"])
        script = Script(
            calls(("get_size_guide", {"category": "Kurta", "gender": "Men"})),
            calls(("send_reply", {"message": "Your chest 39 in → L. *L* is your size. Great choice!"})),
        )
        assistant._complete = script
        reply = send("what size kurta should i get? my chest is 39")
        check("the model's copy of the size is dropped, not sent back", len(script.seen) == 2, len(script.seen))
        check("the chart still says L, once", reply.count("*L* is your size") == 1, reply)

        script = Script(
            calls(("get_size_guide", {"category": "Mojari", "gender": "Men"})),
            calls(("send_reply", {"message": "Here you go."})),
        )
        assistant._complete = script
        reply = send("which size mojari for me, my foot is 26.5 cm")
        check("'Mojari' is the store's Footwear: UK 9 for 26.5 cm", "*UK 9* is your size" in reply, reply)

    cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
