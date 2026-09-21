"""Training the bot: owner ratings change ranking for similar questions, owner
examples reach the assistant, customers' "Not for me" stays personal, and the
admin page is for the owner only.

Every rating here belongs to this script's own users, so deleting them at the
end (ON DELETE CASCADE) removes every trace: no test rating ever leaks into the
real shop's ranking. No LLM calls; WhatsApp is not contacted.

Run from the repo root:  backend/.venv/Scripts/python scripts/test_training.py
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"
os.environ["ADMIN_EMAILS"] = "test-owner@example.com"
os.environ["LLM_API_KEY"] = ""          # plain path unless a test installs a fake model
os.environ["OPENAI_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import repository as repo  # noqa: E402
import training  # noqa: E402
from bot import assistant, retrieval  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import BotReply, CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402

PHONE_A, PHONE_B = "910000000051", "910000000052"
OWNER, STAFF = "test-owner@example.com", "test-staff@example.com"
passed, failed = 0, 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(select(User).where(User.whatsapp_number.in_([PHONE_A, PHONE_B])
                                           | User.email.in_([OWNER, STAFF]))).all()
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
            db.delete(user)       # bot_replies and feedback go with them (CASCADE)
    training._rules_cache["at"] = 0.0


def ids(query: str, user_id=None, k=10) -> list[str]:
    return [p["id"] for p in retrieval.search(query, k=k, user_id=user_id).products]


def main() -> int:
    init_db()
    cleanup()

    with TestClient(app) as web:
        def whatsapp(phone: str, body: str = "", button: str = "") -> dict:
            message = ({"type": "interactive", "interactive": {"type": "button_reply",
                                                               "button_reply": {"id": button, "title": "tap"}}}
                       if button else {"type": "text", "text": {"body": body}})
            payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                "field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": phone, "profile": {"name": "Tester"}}],
                    "messages": [{"from": phone, "id": "wamid.tr", "timestamp": "1", **message}]}}]}]}
            return web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]

        print("\n1. Every free-text answer is kept for review")
        whatsapp(PHONE_A, "sherwani for my wedding")
        user_a = repo.get_or_create_user(PHONE_A, None)
        with session_scope() as db:
            logged = db.exec(select(BotReply).where(BotReply.user_id == user_a)).all()
            logged = [(r.question, r.product_ids, r.path) for r in logged]
        check("the reply is logged with the products shown", logged and logged[-1][0] == "sherwani for my wedding"
              and logged[-1][1] and logged[-1][2] == "search", logged)
        whatsapp(PHONE_A, "CART")
        with session_scope() as db:
            count = len(db.exec(select(BotReply).where(BotReply.user_id == user_a)).all())
        check("commands are not logged (nothing to rate)", count == 1, count)

        print("\n2. Owner: a wrong product is pushed down for similar questions")
        web.post("/signup", data={"email": OWNER, "password": "owner-password-1", "display_name": "Owner"})
        owner_id = web.get("/api/me").json()["user_id"]
        before = ids("sherwani for my wedding")
        wrong = before[0]
        training.rate("owner", -1, "sherwani for my wedding", wrong, None, owner_id)
        check("same question: it is no longer first", ids("sherwani for my wedding")[0] != wrong,
              (before, ids("sherwani for my wedding")))
        check("same question: it drops to the end of the matches", ids("sherwani for my wedding")[-1] == wrong)
        check("reworded question counts too", ids("wedding sherwani for me")[0] != wrong, ids("wedding sherwani for me"))
        unrelated_before = ids("kurti for office", k=5)
        check("an unrelated question is untouched", ids("kurti for office", k=5) == unrelated_before)
        similarity = training._cosine(training._embed("sherwani for my wedding"), training._embed("saree for office"))
        check("different questions are not 'similar'", similarity < training.SIMILAR, similarity)

        print("\n3. Owner: a good match moves up")
        base = ids("jacket to wear over my kurta")
        lifted = base[-1]
        good_id = training.rate("owner", 1, "jacket to wear over my kurta", lifted, None, owner_id)
        check("the product rated good comes first", ids("jacket to wear over my kurta")[0] == lifted,
              (base, ids("jacket to wear over my kurta")))

        print("\n4. Undo restores the original order")
        training.undo(good_id)
        check("back to the meaning-based order", ids("jacket to wear over my kurta") == base)

        print("\n5. Customer 'Not for me' is personal")
        user_b = repo.get_or_create_user(PHONE_B, "Other")
        shown = ids("saree for office", user_id=user_a)
        reply = whatsapp(PHONE_A, button=f"hide:{shown[0]}")
        check("the button reply confirms", "won't show you" in reply["body"], reply["body"])
        check("hidden for that customer", shown[0] not in ids("saree for office", user_id=user_a))
        check("...even for a different question", shown[0] not in ids("something for a party", user_id=user_a, k=40))
        check("not hidden for anyone else", shown[0] in ids("saree for office", user_id=user_b))
        check("not hidden for anonymous website search", shown[0] in ids("saree for office"))
        picked = whatsapp(PHONE_A, button="pick:EW006")
        check("'Choose' button selects the product, with its photo",
              picked["photos"] == ["EW006"] and "Royal Blue Wedding Sherwani" in picked["body"], picked)
        added = whatsapp(PHONE_A, "ADD 42")
        check("...so ADD works next", "Added Royal Blue Wedding Sherwani" in added["body"], added["body"][:60])

        print("\n6. Owner examples and notes reach the assistant")
        with session_scope() as db:
            reply_row = db.exec(select(BotReply).where(BotReply.user_id == user_a)
                                .order_by(BotReply.reply_id)).first()
            reply_id = reply_row.reply_id
        training.rate("owner", 1, "sherwani for my wedding", "", reply_id, owner_id)
        training.rate("owner", -1, "outfit for my brother's haldi", "", None, owner_id, "ask which colour they like first")
        guide = assistant._owner_guidance("sherwani for my big day wedding")
        check("an approved reply is offered as an example", "REPLIES THE OWNER APPROVED" in guide
              and "sherwani for my wedding" in guide, guide[:200])
        check("an owner's note is offered as a rule", "ask which colour" in assistant._owner_guidance(
            "what should my brother wear to the haldi"), assistant._owner_guidance("what should my brother wear to the haldi"))
        # Only this test's own ratings are checked: the real shop may have rated
        # cart questions itself, and that guidance is correct to appear
        unrelated = assistant._owner_guidance("my cart total please")
        check("this test's ratings are not offered for an unrelated question",
              "sherwani for my wedding" not in unrelated and "ask which colour" not in unrelated, unrelated[:200])

        seen = []
        os.environ["LLM_API_KEY"] = "fake"
        assistant._complete = lambda model, messages: (seen.append(messages[0]["content"]) or SimpleNamespace(
            content="", tool_calls=[SimpleNamespace(id="c1", function=SimpleNamespace(
                name="send_reply", arguments=json.dumps({"message": "Here are two I love."})))]))
        whatsapp(PHONE_A, "sherwani for my own wedding")
        os.environ["LLM_API_KEY"] = ""
        check("...inside the prompt the model actually receives", seen and "REPLIES THE OWNER APPROVED" in seen[0])

        print("\n7. The admin page is for the owner only")
        check("signed-out visitors go to sign-in",
              TestClient(app).get("/admin/training", follow_redirects=False).headers.get("location", "").startswith("/login"))
        page = web.get("/admin/training")
        check("the owner sees recent replies", page.status_code == 200 and "sherwani for my wedding" in page.text)
        response = web.post("/admin/training/rate", data={"reply_id": reply_id, "question": "sherwani for my wedding",
                                                          "rating": -1, "product_id": "EW007"}, follow_redirects=False)
        check("rating from the page is saved", response.status_code == 303
              and any(r["product_id"] == "EW007" and r["rating"] == -1 for r in training.learned_rules()))
        learned = web.get("/admin/training?tab=learned")
        check("'What it has learned' lists it", "Push down:" in learned.text and "Champagne Gold Sherwani" in learned.text)
        rule = next(r for r in training.learned_rules() if r["product_id"] == "EW007")
        web.post(f"/admin/training/undo/{rule['id']}", data={"back": "learned"})
        check("Undo from the page removes it", all(r["id"] != rule["id"] for r in training.learned_rules()))

        staff = TestClient(app)
        staff.post("/signup", data={"email": STAFF, "password": "staff-password-1", "display_name": "Staff"})
        check("another signed-in customer gets 403", staff.get("/admin/training").status_code == 403)
        check("...and cannot rate", staff.post("/admin/training/rate", data={
            "reply_id": reply_id, "rating": 1, "product_id": "EW001"}).status_code == 403)

    cleanup()
    leftover = training.ranking_adjustments("sherwani for my wedding")
    check("cleanup leaves no test rating behind", leftover == {}, leftover)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
