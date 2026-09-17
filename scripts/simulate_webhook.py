import argparse
import json

import httpx


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
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = args.message
    response = httpx.post(args.url, json=payload, headers={"X-Local-Test": "true"}, timeout=15)
    print(f"HTTP {response.status_code}")
    print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
