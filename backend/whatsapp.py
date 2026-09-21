import os

import httpx

GRAPH_API_VERSION = "v21.0"

# WhatsApp's limits for interactive cta_url messages
MAX_CTA_BODY = 1024
MAX_CTA_BUTTON = 20


async def _post(to: str, payload: dict) -> dict:
    """Send one message payload to the Graph API, explaining the usual failures."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {os.environ['WHATSAPP_ACCESS_TOKEN']}"}
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, headers=headers, json=body)

    data = response.json()
    if response.is_error:
        print(f"[whatsapp] send failed ({response.status_code}): {data}")
        code = (data.get("error") or {}).get("code")
        if code == 190:
            print(
                "[whatsapp] token expired — generate a new one in the App Dashboard "
                "(WhatsApp > API Setup) and update WHATSAPP_ACCESS_TOKEN in backend/.env"
            )
        elif code == 131030:
            print(
                "[whatsapp] recipient not on the allow list — add the number under "
                "WhatsApp > API Setup > To and confirm the code it sends"
            )
        response.raise_for_status()
    print(f"[whatsapp] sent {payload['type']} to {to}: {data}")
    return data


async def send_whatsapp_message(to: str, body: str) -> dict:
    """Send a plain text message from the test number.

    `to` is the recipient in international format without '+' (e.g. "919876543210").
    Free-form text only delivers inside the 24h window after the user last messaged you;
    outside it Meta returns 200 but the message silently fails (use a template instead).
    """
    return await _post(to, {"type": "text", "text": {"preview_url": False, "body": body}})


async def send_cta_url(to: str, body: str, button_text: str, url: str) -> dict:
    """Send a message with one tappable button that opens a URL — the Pay Now button.

    Same 24-hour window rule as plain text. Body is capped at 1024 characters and
    the button label at 20 by WhatsApp; both are trimmed here rather than rejected.
    """
    return await _post(
        to,
        {
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": body[:MAX_CTA_BODY]},
                "action": {
                    "name": "cta_url",
                    "parameters": {"display_text": button_text[:MAX_CTA_BUTTON], "url": url},
                },
            },
        },
    )
