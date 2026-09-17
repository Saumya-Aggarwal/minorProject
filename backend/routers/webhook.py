import asyncio
import inspect
import os

from fastapi import APIRouter, HTTPException, Request, Response

import repository as repo
from bot.chat import get_product_recommendations
from common.schemas import ProductMatch
from repository import LINK_PREFIX
from whatsapp import send_whatsapp_message

router = APIRouter()


@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta calls this once when you click "Verify and save" in the dashboard."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == os.environ["WHATSAPP_VERIFY_TOKEN"]:
        print("[webhook] verification succeeded")
        # Must echo the challenge back as plain text, not JSON
        return Response(content=challenge, media_type="text/plain")

    print(f"[webhook] verification failed: mode={mode!r}")
    raise HTTPException(status_code=403, detail="Verification failed")


def _recommend(query: str, user_context: str | None):
    """Call the RAG contract, passing user_context only if it accepts one.

    The shared contract is get_product_recommendations(query, top_k). Adding
    user_context is agreed but not yet implemented on the RAG side, so this
    checks the signature instead of assuming — the bot keeps working either way.
    """
    if user_context and "user_context" in inspect.signature(
        get_product_recommendations
    ).parameters:
        return get_product_recommendations(query, user_context=user_context)
    return get_product_recommendations(query)


async def _safe_send(to: str, body: str) -> None:
    """Send without letting a failure escape into the webhook response.

    A failed send must not 500: Meta would treat the delivery as failed and
    redeliver the same inbound message, which fails identically. One lost reply
    beats an infinite retry loop.
    """
    try:
        await send_whatsapp_message(to, body)
    except Exception as exc:
        print(f"[webhook] reply to {to} not delivered: {exc!r}")


def _profile_names(value: dict) -> dict[str, str]:
    """Map wa_id -> profile name from the contacts block, when Meta sends one."""
    names = {}
    for contact in value.get("contacts", []):
        wa_id = contact.get("wa_id")
        name = (contact.get("profile") or {}).get("name")
        if wa_id and name:
            names[wa_id] = name
    return names


async def _handle_link_code(sender: str, text: str) -> str:
    """Bind this number to the website account that minted the code.

    A browser session can never reach us, so this one-time code is the only
    proof of who the sender is. After it succeeds, the number itself is enough.
    """
    token = text[len(LINK_PREFIX):].strip()
    try:
        user = await asyncio.to_thread(repo.consume_link_token, token, sender)
    except Exception as exc:
        print(f"[webhook] link failed: {exc!r}")
        return "Something went wrong linking your account. Please try again from the website."

    if user is None:
        return (
            "That link code is invalid or has expired. "
            "Open your account page on our website and tap Connect WhatsApp for a fresh one."
        )

    name = user.get("display_name") or "there"
    history = await asyncio.to_thread(repo.get_order_history, user["user_id"], 3)

    if history:
        recent = ", ".join(order["product_name"] for order in history)
        return (
            f"Welcome back, {name}! Your account is connected. "
            f"I can see your recent orders: {recent}. What are you shopping for today?"
        )
    return (
        f"Welcome back, {name}! Your account is connected. "
        "Tell me what you are looking for and I will suggest something from our collection."
    )


def _build_user_context(user: dict | None, history: list[dict]) -> str | None:
    """Condense who the customer is and what they own into prompt-ready text."""
    if not user or not user.get("is_linked"):
        return None

    lines = [f"Customer name: {user.get('display_name') or 'unknown'}"]
    if history:
        lines.append("Previously purchased:")
        lines.extend(
            f"- {order['product_name']} (Rs. {order['price_inr']:.0f})" for order in history
        )
    else:
        lines.append("No previous purchases.")
    return "\n".join(lines)


async def _handle_text(sender: str, text: str, display_name: str | None) -> str:
    """Persist the contact, answer the query, and record the turn.

    Database problems must never stop a reply going out — a dead Postgres
    costs us conversation memory, not the conversation.
    """
    if text.strip().upper().startswith(LINK_PREFIX):
        return await _handle_link_code(sender, text.strip())

    user_id = None
    user = None
    history: list[dict] = []
    try:
        user_id = await asyncio.to_thread(repo.get_or_create_user, sender, display_name)
        user = await asyncio.to_thread(repo.get_user_by_whatsapp, sender)
        if user and user.get("is_linked"):
            history = await asyncio.to_thread(repo.get_order_history, user_id, 5)
        previous = await asyncio.to_thread(repo.get_active_session, user_id)
        if previous and previous.get("last_query"):
            print(f"[webhook] prior turn for {sender}: {previous['last_query']!r}")
    except Exception as exc:
        print(f"[webhook] user lookup failed, continuing stateless: {exc!r}")

    user_context = _build_user_context(user, history)
    if user_context:
        print(f"[webhook] personalizing for linked user {user_id}")

    # get_product_recommendations is synchronous and does network I/O.
    # user_context is passed only when the RAG side supports it, so Dev B's
    # current two-argument signature keeps working untouched.
    reply, raw_matches = await asyncio.to_thread(
        _recommend, text, user_context
    )

    # Chroma metadata is unvalidated, so normalize before anything stores it
    matches = [ProductMatch.from_raw(match) for match in raw_matches]

    if user_id is not None:
        try:
            await asyncio.to_thread(
                repo.record_turn,
                user_id,
                text,
                [match.model_dump() for match in matches],
            )
        except Exception as exc:
            print(f"[webhook] could not record turn: {exc!r}")

    return reply


@router.post("/webhook")
async def receive_message(request: Request):
    """Receives incoming messages and status updates from WhatsApp."""
    payload = await request.json()
    local_test = request.headers.get("X-Local-Test") == "true"
    responses = []

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                if "statuses" in value:
                    # sent/delivered/read receipts for messages we sent
                    for status in value["statuses"]:
                        print(f"[webhook] status: {status['status']} for {status['recipient_id']}")

                names = _profile_names(value)

                for message in value.get("messages", []):
                    sender = message["from"]
                    if message["type"] == "text":
                        text = message["text"]["body"]
                        print(f"[webhook] message from {sender}: {text}")
                        reply = await _handle_text(sender, text, names.get(sender))
                        responses.append({"to": sender, "body": reply})
                        if not local_test:
                            await _safe_send(sender, reply)
                    else:
                        print(f"[webhook] unhandled {message['type']!r} message from {sender}")
    except Exception as e:
        # Deliberately broad: any uncaught error here becomes a 500, which Meta
        # reads as failed delivery and retries — the same message arrives again
        # and fails again. Acking 200 is always the right answer.
        print(f"[webhook] handler error: {e!r} payload={payload}")

    return {"status": "received", "responses": responses}
