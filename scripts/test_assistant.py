"""The conversational assistant, driven by a scripted fake LLM.

No network and no quota: bot.assistant._complete is replaced by a script of
model messages, so every check is deterministic. What is checked is the part we
own — tool plumbing, what reaches the customer, what is remembered, and that
every failure falls back to a plain answer. The live model is exercised
separately (see CLAUDE.md, "Assistant").

Creates and removes only its own users.
Run from the repo root:  backend/.venv/Scripts/python scripts/test_assistant.py
"""

import asyncio
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
os.environ["LLM_API_KEY"] = "fake-key-for-tests"
os.environ["LLM_MODEL"] = "fake-primary"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

import repository as repo  # noqa: E402
from bot import assistant, chat  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from models import CartItem, LinkToken, Order, OrderItem, Session, User  # noqa: E402
from routers import webhook  # noqa: E402

PHONE = "910000000041"
OTHER = "910000000042"
WEB_EMAIL = "test-assistant-web@example.com"
passed, failed = 0, 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label} {detail}")


# --- the fake model -------------------------------------------------------------------

def says(text: str):
    return SimpleNamespace(content=text, tool_calls=None)


def calls(*pairs):
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(pairs)])


class Script:
    """Hands out prepared model messages in order and keeps what the model was sent."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []
        self.models: list[str] = []

    def __call__(self, model, messages):
        self.models.append(model)
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        step = self.steps.pop(0) if self.steps else says("(script ran out)")
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def run(script: Script) -> None:
    assistant._complete = script


chat._intro = lambda *args, **kwargs: ""   # the fallback path's one-liner: no network


def cleanup() -> None:
    with session_scope() as db:
        users = db.exec(select(User).where(User.whatsapp_number.in_([PHONE, OTHER])
                                           | (User.email == WEB_EMAIL))).all()
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


def main() -> int:
    init_db()
    cleanup()

    with TestClient(app) as web:
        def send(body: str, phone: str = PHONE) -> str:
            payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                "field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": phone, "profile": {"name": "Asha"}}],
                    "messages": [{"from": phone, "id": "wamid.t", "timestamp": "1", "type": "text",
                                  "text": {"body": body}}]}}]}]}
            return web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]["body"]

        print("\n1. A vague request gets a question, not a product dump")
        script = Script(calls(("send_reply", {"message": "Lovely! Is the outfit for you or a guest, and which function — mehendi, sangeet or the ceremony?"})))
        run(script)
        reply = send("I want something for my wedding")
        check("the customer is asked a question", "which function" in reply, reply)
        check("no product list was attached", "Reply 1" not in reply, reply)
        check("the model saw the customer's message", script.seen[0][-1]["content"] == "I want something for my wedding")

        print("\n2. Memory: the next turn carries the conversation")
        script = Script(
            calls(("search_products", {"query": "groom sherwani for the wedding ceremony", "gender": "Men",
                                       "categories": ["Sherwani"]})),
            lambda messages: calls(("send_reply", {
                "message": "Two sherwanis made for the main ceremony.",
                "product_ids": ["EW006", "EW007"]})),
        )
        run(script)
        reply = send("It's for me, the groom. The main ceremony.")
        history = [m["content"] for m in script.seen[0] if m["role"] in ("user", "assistant")]
        check("previous exchange was sent to the model", any("which function" in h for h in history), history)
        tool_result = next(m["content"] for m in script.seen[1] if m["role"] == "tool")
        check("search tool returned real catalogue ids", "EW006" in tool_result and "Sherwani" in tool_result, tool_result[:120])

        print("\n3. Products shown are rendered by code, from the catalogue")
        check("list numbered in the model's order", reply.index("Royal Blue Wedding Sherwani") < reply.index("Champagne Gold Sherwani"))
        check("prices come from the catalogue", "Rs. 8,999" in reply and "Rs. 11,499" in reply, reply)
        check("command footer added", "Reply 1–2 to choose" in reply)
        session = repo.get_active_session(repo.get_or_create_user(PHONE, None))
        check("list stored for the next turn", [p["product_id"] for p in session["last_products_shown"]] == ["EW006", "EW007"])
        check("the exchange is remembered", any("[Showed: 1. EW006" in m["content"] for m in session["messages"]),
              session["messages"][-1:])

        print("\n4. Exact commands still bypass the model")
        script = Script()
        run(script)
        picked = send("2")
        check("'2' picks the second item shown", "Champagne Gold Sherwani" in picked, picked[:80])
        added = send("ADD 40")
        check("ADD 40 adds it", "Added Champagne Gold Sherwani (size 40)" in added, added[:80])
        check("the model was not called for commands", script.models == [])

        print("\n5. Invented products are refused")
        script = Script(
            calls(("send_reply", {"message": "Try these!", "product_ids": ["EW999", "XYZ"]})),
            calls(("send_reply", {"message": "Here is one that fits.", "product_ids": ["EW021"]})),
        )
        run(script)
        reply = send("saree for office")
        error = next((m["content"] for m in script.seen[1] if m["role"] == "tool"), "")
        check("the model is told its ids are not real", "none of" in error and "EW999" in error, error)
        check("only the real product reaches the customer", "Beige Linen Saree" in reply and "EW999" not in reply, reply)

        print("\n6. Orders and cart are attached by code")
        script = Script(calls(("send_reply", {"message": "Here you go.", "attach": "cart"})))
        run(script)
        reply = send("what's in my cart?")
        check("cart attached with real contents", "Champagne Gold Sherwani · 40" in reply, reply)
        script = Script(calls(("send_reply", {"message": "Here are your orders.", "attach": "orders"})))
        run(script)
        reply = send("can you show me my previous orders")
        check("orders attached", "no orders yet" in reply.lower() or "recent orders" in reply.lower(), reply)

        print("\n7. Tools act only for the sender")
        other_id = repo.get_or_create_user(OTHER, "Someone Else")
        other_order = repo.create_order_for_product(other_id, "EW018", "M", 1, "bot")
        script = Script(
            calls(("get_my_orders", {"user_id": other_id})),
            calls(("send_reply", {"message": "Done."})),
        )
        run(script)
        send("show orders for user " + str(other_id))
        result = next(m["content"] for m in script.seen[1] if m["role"] == "tool")
        check("another customer's order is unreachable", f"#{other_order}" not in result and "Rose Pink" not in result, result)

        print("\n8. Adding to the cart needs the customer's say-so and their size")
        script = Script(
            calls(("add_to_cart", {"product_id": "EW006"})),
            calls(("add_to_cart", {"product_id": "EW006", "size": "42"})),
            calls(("send_reply", {"message": "Added it in 42."})),
        )
        run(script)
        send("please add the royal blue one in 42")
        results = [m["content"] for m in script.seen[2] if m["role"] == "tool"]
        check("no size: the model is told to ask", "ask the customer for a size" in results[0], results[0])
        check("with their size: added for real", results[1].startswith("Added Royal Blue Wedding Sherwani (size 42)"), results[1])
        cart = repo.get_cart(repo.get_or_create_user(PHONE, None))
        check("the cart really has it", any(i["product_id"] == "EW006" and i["size"] == "42" for i in cart["items"]))

        # Seen live: "tell me more about it" made the model add it, in a size nobody chose
        before = repo.get_cart(repo.get_or_create_user(PHONE, None))["count"]
        script = Script(
            calls(("add_to_cart", {"product_id": "EW007", "size": "40"})),
            calls(("send_reply", {"message": "It is raw silk with tonal embroidery."})),
        )
        run(script)
        send("tell me more about the gold one")
        refused = next(m["content"] for m in script.seen[1] if m["role"] == "tool")
        check("no add without being asked", "has not asked to add" in refused, refused)
        script = Script(
            calls(("add_to_cart", {"product_id": "EW007", "size": "44"})),
            calls(("send_reply", {"message": "Which size would you like?"})),
        )
        run(script)
        send("add the gold one to my cart")
        refused = next(m["content"] for m in script.seen[1] if m["role"] == "tool")
        check("no size the customer did not say", "has not said size 44" in refused, refused)
        check("cart unchanged by both", repo.get_cart(repo.get_or_create_user(PHONE, None))["count"] == before)

        print("\n9. Failures fall back to a plain answer")
        script = Script(Exception("429 rate_limit_exceeded"), Exception("503 over capacity"))
        run(script)
        reply = send("saree for office")
        check("both models tried", script.models == ["fake-primary", "fake-fallback"], script.models)
        check("customer still gets products", "Beige Linen Saree" in reply and "Reply 1" in reply, reply[:120])

        script = Script(Exception("Error code: 401 - invalid_api_key"))
        run(script)
        reply = send("sherwani for my wedding")
        check("a rejected key falls back at once", script.models == ["fake-primary"] and "Sherwani" in reply, script.models)

        script = Script(*[calls(("get_my_cart", {}))] * 10)
        run(script)
        reply = send("hmm let me think")
        check("a model that never replies is cut off", len(script.models) == assistant.MAX_ROUNDS, len(script.models))
        check("...and the customer still gets an answer", bool(reply.strip()), reply)

        script = Script(calls(("send_reply", {"message": "**Bold** claim\n## Heading\n[click](https://evil.example)"})))
        run(script)
        reply = send("tell me something")
        check("markdown converted for WhatsApp", "*Bold*" in reply and "**" not in reply and "##" not in reply, reply)
        check("model-written links removed", "evil.example" not in reply, reply)

        print("\n10. Problems first seen with the live model")
        rejected = Exception("Error code: 400 - tool_use_failed")
        rejected.body = {"error": {"code": "tool_use_failed", "failed_generation": json.dumps(
            {"name": "send_reply<|channel|>commentary",
             "arguments": {"message": "Happy to help — who is the outfit for?", "attach": "", "product_ids": None}})}}
        script = Script(rejected)
        run(script)
        reply = send("something for a function")
        check("a tool call Groq rejected is repaired, not dropped", reply.startswith("Happy to help"), reply)
        check("...without a second model call", script.models == ["fake-primary"], script.models)

        script = Script(
            calls(("search_products", {"query": "office saree", "categories": ["Saree"]})),
            says("1. Beige Linen Saree - Rs 2299\n2. Black Georgette Sequin Saree - Rs 3799"),
            calls(("send_reply", {"message": "Two sarees that work for the office.", "product_ids": ["EW021", "EW022"]})),
        )
        run(script)
        reply = send("saree for office please")
        check("a model that lists products itself is asked to attach them",
              "(system) Call send_reply" in script.seen[2][-1]["content"], script.seen[2][-1])
        check("...and the list is then attached and selectable", "Reply 1–2 to choose" in reply, reply)

        script = Script(calls(("send_reply", {"message": "Who is the outfit for?", "attach": "cart"})))
        run(script)
        reply = send("I need an outfit")
        check("cart not attached when the customer did not ask", "cart" not in reply.lower(), reply)

        script = Script(calls(("send_reply", {
            "message": "Great pick for the ceremony:\n1. Royal Blue Wedding Sherwani - Rs 8999",
            "product_ids": ["EW006"]})))
        run(script)
        reply = send("the blue sherwani")
        check("the model's own list line is removed (no duplicate)", reply.count("Royal Blue Wedding Sherwani") == 1, reply)

        script = Script(
            calls(("add_to_cart", {"product_id": "EW006", "size": "44"})),
            calls(("send_reply", {"message": "Added in 44."})),
        )
        run(script)
        repo.record_turn(repo.get_or_create_user(PHONE, None), "sherwani", [
            {"product_id": "EW006", "name": "Royal Blue Wedding Sherwani", "price_inr": 8999, "rich_description": ""},
            {"product_id": "EW007", "name": "Champagne Gold Sherwani", "price_inr": 11499, "rich_description": ""}])
        reply = send("add the blue one in 44")
        check("ADD with nothing picked goes to the assistant, which resolves 'the blue one'",
              script.models and "Added in 44" in reply, reply)

        def send_full(body: str) -> dict:
            payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                "field": "messages", "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": PHONE, "profile": {"name": "Asha"}}],
                    "messages": [{"from": PHONE, "id": "wamid.t", "timestamp": "1", "type": "text",
                                  "text": {"body": body}}]}}]}]}
            return web.post("/webhook", json=payload, headers={"X-Local-Test": "true"}).json()["responses"][0]

        print("\n11. Product photos")
        run(Script(calls(("send_reply", {"message": "Two for the ceremony.", "product_ids": ["EW006", "EW007"]}))))
        response = send_full("sherwani for the ceremony")
        check("a product list comes with one photo per product, in order", response["photos"] == ["EW006", "EW007"],
              response["photos"])
        response = send_full("1")
        check("picking one sends its photo", response["photos"] == ["EW006"], response)

        sent: list[tuple[str, str]] = []

        async def fake_text(to, body):
            sent.append(("text", body))

        async def fake_image(to, path, caption=""):
            if "EW007" in str(path):
                raise RuntimeError("upload refused")
            sent.append(("image", path.name))

        async def fake_card(to, path, caption, buttons):
            if "EW007" in str(path):
                raise RuntimeError("buttons refused")
            sent.append(("card", f"{path.name} {[b[0] for b in buttons]}"))

        # Never reach Meta from a test: every sender is replaced
        webhook.send_whatsapp_message, webhook.send_image, webhook.send_product_card = fake_text, fake_image, fake_card
        import asyncio
        reply = webhook._list_reply("Two for the ceremony.", [repo.catalog.get_by_id("EW006"),
                                                               repo.catalog.get_by_id("EW007")], "Reply 1–2 to choose")
        asyncio.run(webhook._safe_send(PHONE, reply))
        kinds = [k for k, _ in sent]
        check("sent as intro, photo card with buttons, (caption for a failed photo), footer",
              kinds == ["text", "card", "text", "text"] and sent[0][1] == "Two for the ceremony."
              and sent[1][1] == "EW006.jpg ['pick:EW006', 'hide:EW006']" and "Champagne Gold Sherwani" in sent[2][1]
              and sent[3][1] == "Reply 1–2 to choose", sent)
        check("captions carry the catalogue price", "Rs. 11,499" in sent[2][1], sent[2][1])

        print("\n12. Totals and cart facts never come from the model")
        script = Script()
        run(script)
        reply = send("ok whats my total")
        cart = repo.get_cart(repo.get_or_create_user(PHONE, None))
        check("'whats my total' is answered by code", f"Total: Rs. {cart['total_inr']:,.0f}" in reply
              and script.models == [], reply)

        script = Script(
            calls(("send_reply", {"message": "Your current total is ₹17,498. Ready to check out?"})),
            calls(("send_reply", {"message": "Your current total is ₹17,498!"})),
        )
        run(script)
        reply = send("so how much will all of this come to after the discount then")
        feedback = next(m["content"] for m in script.seen[1] if m["role"] == "tool")
        check("an invented amount is sent back to the model", "17,498 does not match" in feedback, feedback)
        check("...and never reaches the customer", "17,498" not in reply, reply)

        script = Script(calls(("send_reply", {
            "message": f"Your cart total is Rs {cart['total_inr']:,.0f}, and the saree is Rs 9,499."})))
        run(script)
        reply = send("remind me what I am spending altogether and on the saree")
        check("real amounts pass the check", f"{cart['total_inr']:,.0f}" in reply and "9,499" in reply, reply)
        context_sent = script.seen[0][0]["content"]
        check("the model is given the real cart", f"TOTAL Rs {cart['total_inr']:.0f}" in context_sent)

        print("\n13. Products the model writes out itself get the real list and photos")
        run(Script(says("EW020 Emerald Kanjivaram Silk Saree – Rs 9499, great for festive.\n\n"
                        "EW019 Navy Sequin Party Lehenga – Rs 7999, perfect for a sangeet.")))
        response = send_full("anniversary gift ideas for my wife")
        check("two products named -> numbered list with photos", response["photos"] == ["EW020", "EW019"], response)
        check("...no raw product codes left", "EW020" not in response["body"].split("\n\n")[0], response["body"][:80])
        picked = send("2")
        check("...and '2' now selects the second", "Navy Sequin Party Lehenga" in picked, picked[:60])

        run(Script(calls(("get_product_details", {"product_id": "EW020"})), says(
            "The Emerald Kanjivaram Silk Saree is pure silk with a gold zari border, Rs 9,499.")))
        response = send_full("tell me more about the kanjivaram")
        check("one product described -> sent with its photo", response["photos"] == ["EW020"], response)
        check("...with a hint to add it", "ADD" in response["body"], response["body"])

        print("\n14. Removing from the cart")
        run(Script(calls(("remove_from_cart", {"product_id": "EW006"})), calls(("send_reply", {"message": "Done."}))))
        send("please remove the royal blue sherwani from my cart")
        cart = repo.get_cart(repo.get_or_create_user(PHONE, None))
        check("removed when asked", all(i["product_id"] != "EW006" for i in cart["items"]), cart["items"])

        print("\n15. Meta redelivering a message is answered once")
        check("first delivery accepted", webhook._first_delivery("wamid.unique-1"))
        check("the same id again is ignored", not webhook._first_delivery("wamid.unique-1"))

        print("\n16. The 22 Sep conversation: who it is for, a profile, picking by name, honest cart claims")
        user = repo.get_or_create_user(PHONE, None)
        repo.forget_profile(user)
        repo.remember_messages(user, [{"role": "user", "content": "thanks"},
                                      {"role": "assistant", "content": "You're welcome!"}])
        script = Script(calls(("send_reply", {"message": "Lovely! Who is it for, and what budget?"})))
        run(script)
        send("umm i wanna buy something for a night wedding")
        check("unknown wearer: the model is told to ask who it is for first",
              "WHO WILL WEAR IT IS NOT KNOWN" in script.seen[0][0]["content"])

        run(Script(calls(("send_reply", {"message": "Men's options it is.", "customer_gender": "Men",
                                         "remember_note": "likes deep jewel tones"}))))
        send("only male options")
        profile = repo.get_profile(user)
        check("'only male options' while shopping for himself is remembered",
              profile["gender"] == "Men" and "likes deep jewel tones" in profile["notes"], profile)

        script = Script(calls(("send_reply", {"message": "Here you go."})))
        run(script)
        send("something for a sangeet")
        prompt = script.seen[0][0]["content"]
        check("next time the profile reaches the model and it is not told to ask again",
              "Shops for themselves as: a man" in prompt and "NOT KNOWN" not in prompt
              and "likes deep jewel tones" in prompt, prompt[-400:])

        other = repo.get_or_create_user(OTHER, None)
        run(Script(calls(("send_reply", {"message": "Sure."}))))
        send("haldi outfit for my brother", phone=OTHER)
        run(Script(calls(("send_reply", {"message": "Men's then.", "customer_gender": "Men"}))))
        send("only male options", phone=OTHER)
        check("...but not when the outfit is for someone else", repo.get_profile(other)["gender"] is None)

        run(Script(calls(("search_products", {"query": "sherwani for a night wedding", "gender": "Men",
                                              "categories": ["Sherwani"]})),
                   lambda m: calls(("send_reply", {"message": "Evening sherwanis.",
                                                   "product_ids": ["EW039", "EW054", "EW042"]}))))
        send("sherwani for a night wedding")
        script = Script()          # any model call now would be a failure
        run(script)
        picked = send("choose blush pink one")
        check("'choose blush pink one' picks it by name, no model call",
              "Blush Pink Groom Sherwani" in picked and "Sizes:" in picked and not script.seen, picked[:80])
        check("'XL' for a chest-size sherwani is refused with the real sizes",
              "comes in 38, 40, 42, 44, 46" in send("XL"))
        added = send("46")
        check("'46' adds it in 46", "Added Blush Pink Groom Sherwani with Dupatta (size 46)" in added, added[:90])

        repo.clear_cart(user)
        repo.remember_messages(user, [{"role": "user", "content": "I like the royal blue one in 42"},
                                      {"role": "assistant", "content": "Shall I add the Royal Blue Wedding "
                                                                       "Sherwani in 42 to your cart?"}])
        script = Script(says("Added it to your cart!"),
                        calls(("add_to_cart", {"product_id": "EW006", "size": "42"})),
                        calls(("send_reply", {"message": "Done, it is in your cart."})))
        run(script)
        send("yes")
        check("'yes' to 'shall I add it?' lets the add through",
              [(i["product_id"], i["size"]) for i in repo.get_cart(user)["items"]] == [("EW006", "42")],
              repo.get_cart(user)["items"])
        check("...after the model was caught claiming an add it had not done",
              any("nothing was added" in str(m.get("content")) for m in script.seen[1]))

        repo.clear_cart(user)
        run(Script(says("Added it!"), says("Great news, I've added it for you.")))
        honest = send("hmm looks nice")
        check("a repeated false 'added' never reaches the customer",
              "added" not in honest.lower() or "not added" in honest.lower(), honest)
        check("...and nothing is in the cart", not repo.get_cart(user)["items"])

        print("\n17. The customer can see and clear what is remembered")
        browser = TestClient(app)
        browser.post("/signup", data={"email": WEB_EMAIL, "password": "web-password-1", "display_name": "Web"})
        web_user = browser.get("/api/me").json()["user_id"]
        repo.remember_about(web_user, "Women", "prefers pastel colours")
        page = browser.get("/account").text
        check("/account shows what the assistant remembers",
              "What our assistant remembers" in page and "Prefers pastel colours" in page and "womenswear" in page)
        browser.post("/account/forget")
        check("'Forget this' clears it", repo.get_profile(web_user) == {"gender": None, "notes": []})

        # Live: the bot asked "which size?", the customer answered "i think medium
        # would look good on her", and the add was refused as unasked-for
        repo.clear_cart(user)
        repo.remember_messages(user, [
            {"role": "user", "content": "would 1 be a good gift for my wife?"},
            {"role": "assistant", "content": "The Crimson Anarkali Suit is a lovely gift. Which size?"}])
        run(Script(calls(("add_to_cart", {"product_id": "EW017", "size": "M"})),
                   calls(("send_reply", {"message": "Added for you."}))))
        send("i think medium would look good on her")
        check("a size in words, answering our own question, is allowed through",
              [(i["product_id"], i["size"]) for i in repo.get_cart(user)["items"]] == [("EW017", "M")],
              repo.get_cart(user)["items"])

        print("\n18. It is a shop assistant, not a chatbot")
        run(Script(calls(("send_reply", {
            "message": "Sure! Here's a quick Python snippet:\n\npython\nprint(2+2)\n\nHappy coding!"}))))
        coded = send("umm can u write a python code for 2+2")
        check("code never reaches the customer",
              "print(" not in coded and "only help with our ethnic wear" in coded, coded[:120])
        run(Script(calls(("send_reply", {"message": "We do not sell those, but tell me what you are shopping for."}))))
        check("an ordinary off-topic question is still answered kindly",
              "shopping for" in send("do we have bombs?"))

        print("\n19. Blue ticks and 'typing…' while the answer is being worked out")
        import whatsapp
        check("one call marks the message read and starts typing",
              whatsapp.typing_payload("wamid.abc") == {
                  "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.abc",
                  "typing_indicator": {"type": "text"}})
        order: list[str] = []
        real_typing, real_send = webhook.mark_read_and_typing, webhook._safe_send

        async def fake_typing(message_id):
            order.append(f"typing:{message_id}")

        async def fake_send(to, reply):
            order.append("reply")

        webhook.mark_read_and_typing, webhook._safe_send = fake_typing, fake_send
        run(Script(calls(("send_reply", {"message": "On it!"}))))
        asyncio.run(webhook._answer_and_send(PHONE, "anything for diwali?", "Asha", "", "wamid.xyz"))
        webhook.mark_read_and_typing, webhook._safe_send = real_typing, real_send
        check("typing starts before the answer is sent", order == ["typing:wamid.xyz", "reply"], order)

    cleanup()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
