import os
import asyncio

from fastapi import APIRouter, HTTPException, Request, Response

from bot.chat import get_product_recommendations
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

                for message in value.get("messages", []):
                    sender = message["from"]
                    if message["type"] == "text":
                        text = message["text"]["body"]
                        print(f"[webhook] message from {sender}: {text}")
                        reply, _ = await asyncio.to_thread(get_product_recommendations, text)
                        responses.append({"to": sender, "body": reply})
                        if not local_test:
                            await send_whatsapp_message(sender, reply)
                    else:
                        print(f"[webhook] unhandled {message['type']!r} message from {sender}")
    except (KeyError, TypeError, ValueError) as e:
        # Always ack with 200, otherwise Meta keeps retrying the same payload
        print(f"[webhook] parse error: {e!r} payload={payload}")

    return {"status": "received", "responses": responses}
