"""The tracing behind the P2 eval harness must not change what a customer gets.

One fixed conversation is played through every exit of the webhook (commands,
selection, sizes, BUY, CHECKOUT and the address questions, lookups, buy it
again, buttons, linking, the assistant with each of its checks, the plain
search fallback, the background path that answers real messages) with a fake
model at the LOWEST layer, llm.client, so llm.complete and the assistant's
retries, fallbacks and repairs all run for real. Per turn it records:
  * the reply (as the local-test webhook returns it, or what the background
    path handed to WhatsApp)
  * every request the fake model received: model, messages, options
  * the database state the bot reads on the next turn, without ids or times

Modes (run from the repo root):
  (no arguments)     play it twice in one process: normally, then with every
                     internal of tracing.py failing. Both must be identical.
                     Part of run_all_tests.py.
  --record FILE      write one run to FILE. Used once, on the code from before
                     the instrumentation, to have something to compare with.
  --compare FILE     this code's run must equal the one in FILE.

Creates and removes only its own users. No network: payments, WhatsApp and
the model are all fakes.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
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
os.environ["LLM_API_KEY"] = "fake-key-for-tests"
os.environ["LLM_MODEL"] = "fake-primary"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"
os.environ["LLM_REASONING_EFFORT"] = "low"
os.environ["PUBLIC_BASE_URL"] = "https://neutrality.invalid"
for name in [k for k in os.environ if k.startswith("LLM_CHAT_")]:
    del os.environ[name]

import httpx  # noqa: E402
import openai  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openai.types.chat import ChatCompletion  # noqa: E402
from sqlalchemy import text  # noqa: E402

import llm  # noqa: E402
import payments  # noqa: E402
import repository as repo  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from main import app  # noqa: E402
from routers import webhook  # noqa: E402

try:
    import tracing  # noqa: E402  (absent on the code from before the instrumentation)
except ImportError:
    tracing = None

SAVED = {"rows": 0}
PHONES = {"A": "910000000071", "B": "910000000072", "C": "910000000073"}
WEB_EMAIL = "neutrality-web@example.com"


# --- the fake model -----------------------------------------------------------------

def completion(content=None, calls=(), usage="full"):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args)}}
                                 for i, (name, args) in enumerate(calls)]
    body = {"id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}]}
    if usage == "full":
        body["usage"] = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                         "prompt_tokens_details": {"cached_tokens": 200},
                         "completion_tokens_details": {"reasoning_tokens": 20}}
    elif usage == "bare":
        body["usage"] = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}
    return ChatCompletion.model_validate(body)


def says(content, usage="full"):
    return completion(content=content, usage=usage)


def calls(*pairs, usage="full"):
    return completion(calls=pairs, usage=usage)


def _response(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://fake.invalid/v1/chat/completions"),
                          headers={"retry-after": "0"})


def rate_limited():
    return openai.RateLimitError("Error code: 429 - Rate limit reached on tokens per minute (TPM). "
                                 "Please try again in 0.3s.", response=_response(429), body=None)


def overloaded():
    return openai.InternalServerError("Error code: 503 - over capacity", response=_response(503), body=None)


def bad_key():
    return openai.AuthenticationError("Error code: 401 - invalid_api_key", response=_response(401), body=None)


def tool_use_failed(generation=None):
    error = {"code": "tool_use_failed", "message": "Failed to parse tool call arguments as JSON"}
    if generation is not None:
        error["failed_generation"] = json.dumps(generation)
    return openai.BadRequestError(f"Error code: 400 - {{'error': {error}}}", response=_response(400),
                                  body={"error": error})


class FakeModel:
    """Hands out scripted responses in order and keeps every request."""

    def __init__(self):
        self.steps: list = []
        self.requests: list[dict] = []

    def respond(self, kwargs):
        self.requests.append({
            "model": kwargs.get("model"),
            "messages": json.loads(json.dumps(kwargs.get("messages"), default=str)),
            "options": {k: v for k, v in sorted(kwargs.items()) if k not in ("model", "messages", "tools")},
            "tools": len(kwargs.get("tools") or []),
        })
        step = self.steps.pop(0) if self.steps else says("(script ran out)")
        if isinstance(step, Exception):
            raise step
        return step

    # The shape of OpenAI(...): .chat.completions.create and .with_raw_response.create
    @property
    def chat(self):
        model = self

        class Raw:
            def create(self, **kwargs):
                parsed = model.respond(kwargs)
                return SimpleNamespace(parse=lambda: parsed, headers=httpx.Headers(
                    {"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-remaining-requests": "990"}))

        class Completions:
            with_raw_response = Raw()

            def create(self, **kwargs):
                return model.respond(kwargs)

        return SimpleNamespace(completions=Completions())


MODEL = FakeModel()
llm.client = lambda role="chat", timeout=12.0: MODEL


# --- payments and WhatsApp: never the network --------------------------------------

_orders = {"n": 0}


async def fake_create_checkout_order(order_id, amount_inr, description, notes=None):
    _orders["n"] += 1
    return {"id": f"order_neutral_{order_id}"}


async def fake_fetch_order_payments(rzp_id):
    raise payments.PaymentError("not in this test")


payments.create_checkout_order = fake_create_checkout_order
payments.fetch_order_payments = fake_fetch_order_payments

SENT: list = []


async def _record(kind, to, *parts):
    SENT.append([kind, *[str(p) for p in parts]])
    return {}


async def fake_text(to, body):
    await _record("text", to, body)


async def fake_cta(to, body, label, url):
    await _record("cta", to, body, label, url)


async def fake_image(to, path, caption=""):
    await _record("image", to, getattr(path, "name", path), caption)


async def fake_card(to, path, caption, buttons):
    await _record("card", to, getattr(path, "name", path), caption, [b[0] for b in buttons])


async def fake_typing(message_id):
    await _record("typing", "", message_id)


webhook.send_whatsapp_message = fake_text
webhook.send_cta_url = fake_cta
webhook.send_image = fake_image
webhook.send_product_card = fake_card
webhook.mark_read_and_typing = fake_typing


# --- the database, without ids and times ----------------------------------------------

def cleanup() -> None:
    with session_scope() as db:
        if tracing is not None:
            # A turn with no customer (an invalid link code) leaves a trace with no user
            db.execute(text("DELETE FROM turn_traces WHERE user_id IS NULL "
                            "AND wa_message_id IN ('wamid.t', 'wamid.neutral')"))
        ids = [r[0] for r in db.execute(text(
            "SELECT user_id FROM users WHERE whatsapp_number = ANY(:p) OR email = :e"),
            {"p": list(PHONES.values()), "e": WEB_EMAIL}).all()]
        if not ids:
            return
        p = {"ids": ids}
        if tracing is not None:
            db.execute(text("DELETE FROM turn_traces WHERE user_id = ANY(:ids)"), p)
        db.execute(text("DELETE FROM order_items WHERE order_id IN "
                        "(SELECT order_id FROM orders WHERE user_id = ANY(:ids))"), p)
        for table in ("orders", "cart_items", "sessions", "link_tokens", "feedback", "bot_replies"):
            db.execute(text(f"DELETE FROM {table} WHERE user_id = ANY(:ids)"), p)
        db.execute(text("DELETE FROM users WHERE user_id = ANY(:ids)"), p)


def _rows(db, sql, params):
    return [dict(r._mapping) for r in db.execute(text(sql), params).all()]


def state() -> dict:
    """Everything the bot reads back, per test customer, ids and timestamps removed."""
    out = {}
    with session_scope() as db:
        users = _rows(db, "SELECT user_id, whatsapp_number, email, display_name, profile, shipping FROM users "
                          "WHERE whatsapp_number = ANY(:p) OR email = :e ORDER BY whatsapp_number, email",
                      {"p": list(PHONES.values()), "e": WEB_EMAIL})
        for user in users:
            uid = {"u": user.pop("user_id")}
            key = user["whatsapp_number"] or user["email"]
            orders = _rows(db, "SELECT order_id, channel, status, total_inr, from_cart, payment_link_url, "
                               "ship_name, ship_phone, ship_line1, ship_line2, ship_city, ship_state, ship_pincode "
                               "FROM orders WHERE user_id = :u ORDER BY order_id", uid)
            for order in orders:
                order["items"] = _rows(db, "SELECT product_id, product_name, price_inr, size, quantity, variant_id "
                                           "FROM order_items WHERE order_id = :o ORDER BY order_item_id",
                                       {"o": order["order_id"]})
            out[key] = {
                "user": user,
                "cart": _rows(db, "SELECT product_id, size, quantity, variant_id FROM cart_items "
                                  "WHERE user_id = :u ORDER BY added_at, cart_item_id", uid),
                "sessions": _rows(db, "SELECT last_query, last_products_shown, selected_product, messages, "
                                      "pending_address, reorder, status FROM sessions WHERE user_id = :u "
                                      "ORDER BY session_id", uid),
                "orders": orders,
                "bot_replies": _rows(db, "SELECT question, answer, product_ids, path FROM bot_replies "
                                         "WHERE user_id = :u ORDER BY reply_id", uid),
                "feedback": _rows(db, "SELECT scope, rating, product_id, question, note FROM feedback "
                                      "WHERE user_id = :u ORDER BY feedback_id", uid),
            }
    return out


# --- the conversation ----------------------------------------------------------------

def _pay_last_order(phone: str) -> None:
    user_id = repo.get_or_create_user(phone, None)
    history = repo.get_order_history(user_id, 1)
    repo.mark_order_paid(history[0]["order_id"], "pay_neutral")


def _remember(phone: str, pairs: list[tuple[str, str]]) -> None:
    user_id = repo.get_or_create_user(phone, None)
    repo.remember_messages(user_id, [{"role": r, "content": c} for r, c in pairs])


def _link_code() -> str:
    with session_scope() as db:
        uid = db.execute(text("SELECT user_id FROM users WHERE email = :e"), {"e": WEB_EMAIL}).scalar()
        if uid is None:
            uid = db.execute(text("INSERT INTO users (store_id, email, display_name, created_at, last_active_at) "
                                  "VALUES (1, :e, 'Web Neutral', now(), now()) RETURNING user_id"),
                             {"e": WEB_EMAIL}).scalar()
    return "LINK-" + repo.create_link_token(uid)


ROYAL_BLUE_AND_GOLD = ["EW006", "EW007"]

TURNS = [
    # who, message, model steps, options
    ("A", "hi", []),
    ("A", "help", []),
    ("A", "I want something for my wedding",
     [calls(("send_reply", {"message": "Lovely! Is it for you or a guest, and which function?"}))]),
    ("A", "It's for me, the groom. The main ceremony.",
     [calls(("search_products", {"query": "groom sherwani for the wedding ceremony", "gender": "Men",
                                 "categories": ["Sherwani"]})),
      calls(("send_reply", {"message": "Two sherwanis made for the main ceremony.",
                            "product_ids": ROYAL_BLUE_AND_GOLD}), usage="bare")]),
    ("A", "2", []),
    ("A", "ADD 40", []),
    ("A", "CART", []),
    ("A", "what's in my cart?", []),
    ("A", "saree for office",
     [calls(("send_reply", {"message": "Try these!", "product_ids": ["EW999", "XYZ"]})),
      calls(("send_reply", {"message": "Here is one that fits.", "product_ids": ["EW021"]}), usage="none")]),
    ("A", "can you show me my previous orders", []),
    ("A", "please add the royal blue one in 42",
     [calls(("add_to_cart", {"product_id": "EW006"})),
      calls(("add_to_cart", {"product_id": "EW006", "size": "42"})),
      calls(("send_reply", {"message": "Added it in 42."}))]),
    ("A", "tell me more about the gold one",
     [calls(("add_to_cart", {"product_id": "EW007", "size": "40"})),
      calls(("send_reply", {"message": "It is raw silk with tonal embroidery."}))]),
    ("A", "saree for office",
     [rate_limited(), overloaded(),
      calls(("send_reply", {"message": "Office sarees.", "product_ids": ["EW021"]}))]),
    ("A", "sherwani for my wedding", [bad_key(), says("A few sherwanis for the big day.")]),
    ("A", "hmm let me think", [calls(("get_my_cart", {}))] * 5),
    ("A", "tell me something",
     [calls(("send_reply", {"message": "**Bold** claim\n## Heading\n[click](https://evil.example)"}))]),
    ("A", "something for a function",
     [tool_use_failed({"name": "send_reply<|channel|>commentary",
                       "arguments": {"message": "Happy to help — who is the outfit for?", "attach": "",
                                     "product_ids": None}})]),
    ("A", "a kurta for diwali",
     [tool_use_failed(), tool_use_failed(),
      calls(("send_reply", {"message": "Festive kurtas.", "product_ids": ["EW045"]}))]),
    ("A", "saree for office please",
     [calls(("search_products", {"query": "office saree", "categories": ["Saree"]})),
      says("1. Beige Linen Saree - Rs 2299\n2. Black Georgette Sequin Saree - Rs 3799"),
      calls(("send_reply", {"message": "Two sarees that work for the office.", "product_ids": ["EW021", "EW022"]}))]),
    ("A", "I need an outfit", [calls(("send_reply", {"message": "Who is the outfit for?", "attach": "cart"}))]),
    ("A", "the blue sherwani",
     [calls(("send_reply", {"message": "Great pick for the ceremony:\n1. Royal Blue Wedding Sherwani - Rs 8999",
                            "product_ids": ["EW006"]}))]),
    ("A", "EW042", []),
    ("A", "XL", []),
    ("A", "46", []),
    ("A", "EW045", []),
    ("A", "BUY M", []),
    ("A", "CHECKOUT", []),
    ("A", "Asha Verma, 9876501234\n12 MG Road, Indiranagar\nBengaluru 560038, Karnataka", []),
    ("A", "CHECKOUT", []),
    ("A", "my address", []),
    ("A", "change my address", []),
    ("A", "cancel", []),
    ("A", "ok whats my total", []),
    ("A", "so how much will all of this come to after the discount then",
     [calls(("send_reply", {"message": "Your current total is ₹17,498. Ready to check out?"})),
      calls(("send_reply", {"message": "Your current total is ₹17,498!"}))]),
    ("A", "hmm looks nice", [says("Added it!"), says("Great news, I've added it for you.")]),
    ("A", "umm can u write a python code for 2+2",
     [calls(("send_reply", {"message": "Sure! Here's a snippet:\n\nprint(2+2)\n\nHappy coding!"}))]),
    ("A", "anniversary gift ideas for my wife",
     [says("EW020 Emerald Kanjivaram Silk Saree – Rs 9499, great for festive.\n\n"
           "EW019 Navy Sequin Party Lehenga – Rs 7999, perfect for a sangeet.")]),
    ("A", "2", []),
    ("A", "tell me more about the kanjivaram",
     [calls(("get_product_details", {"product_id": "EW020"})),
      says("The Emerald Kanjivaram Silk Saree is pure silk with a gold zari border, Rs 9,499.")]),
    ("A", "please remove the royal blue sherwani from my cart",
     [calls(("remove_from_cart", {"product_id": "EW006"})), calls(("send_reply", {"message": "Done."}))]),
    ("A", "not the second one, too flashy",
     [calls(("hide_product", {"product_id": "EW019", "reason": "too flashy"})),
      calls(("send_reply", {"message": "Hidden. Want calmer options?"}))]),
    ("A", "only male options",
     [calls(("send_reply", {"message": "Men's options it is.", "customer_gender": "Men",
                            "remember_note": "likes deep jewel tones"}))]),
    ("A", "would 1 be a good gift for my wife?",
     [calls(("send_reply", {"message": "Yes, a lovely gift. Which size?"}))]),
    ("A", "i think medium would look good on her", []),
    ("A", "can i buy one my old order again", [], {"before": lambda: _pay_last_order(PHONES["A"])}),
    ("A", "more", []),
    ("A", "1", []),
    ("A", "yes", [calls(("add_to_cart", {"product_id": "EW006", "size": "42"})),
                  calls(("send_reply", {"message": "Done, it is in your cart."}))],
     {"before": lambda: _remember(PHONES["A"], [("user", "I like the royal blue one in 42"),
                                               ("assistant", "Shall I add the Royal Blue Wedding Sherwani "
                                                             "in 42 to your cart?")])}),
    ("A", "", [], {"button": "pick:EW006"}),
    ("A", "", [], {"button": "hide:EW007"}),
    ("A", "anything for diwali?",
     [calls(("send_reply", {"message": "On it!", "product_ids": ["EW045", "EW020"]}))], {"via": "background"}),
    ("A", "where is my order", [], {"via": "background"}),
    ("A", "", [], {"media": "image"}),
    ("B", "hi", []),
    ("B", "kurta for haldi", [calls(("send_reply", {"message": "Bright kurtas.", "product_ids": ["EW045"]}))]),
    ("B", "LINK-not-a-real-code", []),
    ("B", None, [], {"link": True}),
    ("B", "CART", []),
    ("C", "hi i would like to buy something to wear on a wedding of my friend",
     [calls(("send_reply", {"message": "Menswear or womenswear, and what budget?"}))]),
    ("C", "male", [calls(("send_reply", {"message": "Menswear then.", "customer_gender": "Men"}))]),
]


def _scrub(value, order_ids: dict[str, str]):
    """Replace ids that differ between runs (orders, link codes) with stable names."""
    if isinstance(value, str):
        # Only where an order id can appear, never a bare number (a price or a size)
        for real, name in order_ids.items():
            value = re.sub(rf"(#|/pay/|/orders/|order_neutral_|[Oo]rder\s#?){real}\b", rf"\g<1>{name}", value)
        return re.sub(r"LINK-[A-Za-z0-9_-]{6,}", "LINK-<code>", value)
    if isinstance(value, list):
        return [_scrub(v, order_ids) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, order_ids) for k, v in value.items()}
    if hasattr(value, "as_tuple"):          # Decimal
        return str(value)
    return value


def play(faults: bool = False) -> list[dict]:
    if tracing is not None:
        tracing._faults = faults          # every internal of tracing.py raises
    cleanup()
    started = datetime.now(timezone.utc)
    out: list[dict] = []
    order_names: dict[str, str] = {}
    with TestClient(app) as web:
        for n, turn in enumerate(TURNS):
            who, message, steps = turn[0], turn[1], turn[2]
            options = turn[3] if len(turn) > 3 else {}
            phone = PHONES[who]
            if options.get("before"):
                options["before"]()
            if options.get("link"):
                message = _link_code()
            MODEL.steps, MODEL.requests = list(steps), []
            SENT.clear()
            response: dict = {}
            if options.get("via") == "background":
                asyncio.run(webhook._answer_and_send(phone, message, "Asha", "", "wamid.neutral"))
            else:
                if options.get("button"):
                    msg = {"from": phone, "id": "wamid.t", "timestamp": "1", "type": "interactive",
                           "interactive": {"type": "button_reply",
                                           "button_reply": {"id": options["button"], "title": "Choose"}}}
                elif options.get("media"):
                    msg = {"from": phone, "id": "wamid.t", "timestamp": "1", "type": "image",
                           "image": {"id": "media-neutral", "mime_type": "image/jpeg"}}
                else:
                    msg = {"from": phone, "id": "wamid.t", "timestamp": "1", "type": "text", "text": {"body": message}}
                payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
                    "field": "messages", "value": {"messaging_product": "whatsapp",
                                                   "contacts": [{"wa_id": phone, "profile": {"name": "Asha"}}],
                                                   "messages": [msg]}}]}]}
                result = web.post("/webhook", json=payload, headers={"X-Local-Test": "true"})
                response = {"status": result.status_code, "body": result.json()}
            snapshot = state()
            for row in snapshot.values():
                for order in row["orders"]:
                    key = str(order.pop("order_id"))
                    order_names.setdefault(key, f"<order{len(order_names) + 1}>")
            out.append(_scrub({"turn": n, "who": who, "message": message, "response": response,
                               "sent": list(SENT), "requests": MODEL.requests,
                               "steps_left": len(MODEL.steps), "state": snapshot}, order_names))
    if tracing is not None:
        with session_scope() as db:
            SAVED["rows"] = db.execute(text(
                "SELECT count(*) FROM turn_traces WHERE wa_message_id IN ('wamid.t', 'wamid.neutral') "
                "AND (user_id IS NULL OR user_id IN (SELECT user_id FROM users "
                "WHERE whatsapp_number = ANY(:p) OR email = :e)) AND created_at >= :since"),
                {"p": list(PHONES.values()), "e": WEB_EMAIL, "since": started}).scalar()
    cleanup()
    if tracing is not None:
        tracing._faults = False
    return out


def _first_difference(a: list[dict], b: list[dict]) -> str:
    for left, right in zip(a, b):
        if left != right:
            for key in left:
                if left.get(key) != right.get(key):
                    return (f"turn {left['turn']} ({left['who']}: {left['message']!r}) differs in {key}:\n"
                            f"  {json.dumps(left.get(key), ensure_ascii=False, default=str)[:900]}\n"
                            f"  {json.dumps(right.get(key), ensure_ascii=False, default=str)[:900]}")
    return f"different number of turns: {len(a)} vs {len(b)}" if len(a) != len(b) else ""


def main() -> int:
    init_db()
    if len(sys.argv) == 3 and sys.argv[1] == "--record":
        Path(sys.argv[2]).write_text(json.dumps(play(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"recorded {len(TURNS)} turns to {sys.argv[2]}")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--compare":
        golden = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        now = json.loads(json.dumps(play(), ensure_ascii=False, default=str))
        diff = _first_difference(golden, now)
        print(f"FAIL  {diff}" if diff else f"PASS  all {len(TURNS)} turns identical to {sys.argv[2]}")
        print(f"\n{0 if diff else 1} passed, {1 if diff else 0} failed")
        return 1 if diff else 0

    passed = failed = 0
    traces: list = []
    if tracing is not None:
        tracing.listeners.append(traces.append)
    normal = json.loads(json.dumps(play(), ensure_ascii=False, default=str))
    if tracing is not None:
        tracing.listeners.remove(traces.append)
    reached = sum(1 for t in normal if t["requests"]) if normal else 0
    checks = [("every turn ran", len(normal) == len(TURNS), len(normal)),
              ("the fake model was reached on many turns", reached >= 25, reached),
              ("every scripted model step was used", all(t["steps_left"] == 0 for t in normal),
               [t["turn"] for t in normal if t["steps_left"]])]
    if tracing is not None:
        checks.append(("every turn was traced, naming the exit that answered it",
                       len(traces) == len(TURNS) and all(t.path for t in traces),
                       [(n, t.path) for n, t in enumerate(traces) if not t.path] or len(traces)))
        checks.append(("every trace was saved to turn_traces", SAVED["rows"] == len(TURNS), SAVED["rows"]))
        faulty = json.loads(json.dumps(play(faults=True), ensure_ascii=False, default=str))
        diff = _first_difference(normal, faulty)
        checks.append(("with every tracing internal failing, customers get exactly the same", not diff, diff))
    for label, ok, detail in checks:
        if ok:
            passed += 1
            print(f"  PASS  {label}")
        else:
            failed += 1
            print(f"  FAIL  {label} {detail}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
