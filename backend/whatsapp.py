import os

import httpx

GRAPH_API_VERSION = "v21.0"


async def send_whatsapp_message(to: str, body: str) -> dict:
    """Send a plain text message from the test number.

    `to` is the recipient in international format without '+' (e.g. "919876543210").
    Free-form text only delivers inside the 24h window after the user last messaged you;
    outside it Meta returns 200 but the message silently fails (use a template instead).
    """
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {os.environ['WHATSAPP_ACCESS_TOKEN']}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, headers=headers, json=payload)

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
    print(f"[whatsapp] sent to {to}: {data}")
    return data
