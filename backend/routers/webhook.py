import asyncio
import os

from fastapi import APIRouter, HTTPException, Request, Response

import repository as repo
from bot.chat import get_product_recommendations
from common.schemas import ProductMatch
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


def _profile_names(value: dict) -> dict[str, str]:
    """Map wa_id -> profile name from the contacts block, when Meta sends one."""
    names = {}
    for contact in value.get("contacts", []):
        wa_id = contact.get("wa_id")
        name = (contact.get("profile") or {}).get("name")
        if wa_id and name:
            names[wa_id] = name
    return names


async def _handle_text(sender: str, text: str, display_name: str | None) -> str:
    """Persist the contact, answer the query, and record the turn.

    Database problems must never stop a reply going out — a dead Postgres
    costs us conversation memory, not the conversation.
    """
    user_id = None
    try:
        user_id = await asyncio.to_thread(repo.get_or_create_user, sender, display_name)
        previous = await asyncio.to_thread(repo.get_active_session, user_id)
        if previous and previous.get("last_query"):
            print(f"[webhook] prior turn for {sender}: {previous['last_query']!r}")
    except Exception as exc:
        print(f"[webhook] user lookup failed, continuing stateless: {exc!r}")

    # get_product_recommendations is synchronous and does network I/O
    reply, raw_matches = await asyncio.to_thread(get_product_recommendations, text)

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
                            await send_whatsapp_message(sender, reply)
                    else:
                        print(f"[webhook] unhandled {message['type']!r} message from {sender}")
    except (KeyError, TypeError, ValueError) as e:
        # Always ack with 200, otherwise Meta keeps retrying the same payload
        print(f"[webhook] parse error: {e!r} payload={payload}")

    return {"status": "received", "responses": responses}
