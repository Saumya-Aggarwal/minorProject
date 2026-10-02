"""Send a fake WhatsApp message to a running server, as Meta would.

It goes to the demo store: the payload carries the demo's phone number id from
backend/.env (or no metadata, which also means the demo store), and is signed
with WHATSAPP_APP_SECRET when that is set, exactly as Meta signs real ones.
X-Local-Test makes the server return the reply instead of sending it.

    backend/.venv/Scripts/python scripts/simulate_webhook.py --message "saree for office"
"""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / "backend" / ".env")


PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "TEST_WABA_ID",
            "changes": [
                {
                    "field": "messages",
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550000000",
                            "phone_number_id": "TEST_PHONE_NUMBER_ID",
                        },
                        "contacts": [{"profile": {"name": "Local Test User"}, "wa_id": "919876543210"}],
                        "messages": [
                            {
                                "from": "919876543210",
                                "id": "wamid.LOCAL_TEST_MESSAGE",
                                "timestamp": "1700000000",
                                "text": {"body": "Show me a silk kurta under 3000"},
                                "type": "text",
                            }
                        ],
                    },
                }
            ],
        }
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a local Meta WhatsApp webhook event.")
    parser.add_argument("--url", default="http://localhost:8000/webhook")
    parser.add_argument("--message", default="Show me a silk kurta under 3000")
    args = parser.parse_args()

    payload = json.loads(json.dumps(PAYLOAD))
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"][0]["text"]["body"] = args.message
    # The demo store's number, or no metadata at all (also the demo store); a
    # made-up number id belongs to no store and would be ignored
    if os.getenv("WHATSAPP_PHONE_NUMBER_ID"):
        value["metadata"]["phone_number_id"] = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    else:
        del value["metadata"]

    body = json.dumps(payload).encode()
    headers = {"X-Local-Test": "true", "Content-Type": "application/json"}
    secret = os.getenv("WHATSAPP_APP_SECRET")
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = httpx.post(args.url, content=body, headers=headers, timeout=15)
    print(f"HTTP {response.status_code}")
    print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
