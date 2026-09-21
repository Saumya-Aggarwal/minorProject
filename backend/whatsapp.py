import json
import os
import time
from pathlib import Path

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


# --- product photos ------------------------------------------------------------------

MAX_CAPTION = 1024
MEDIA_CACHE = Path(__file__).resolve().parent / ".media_cache.json"
MEDIA_TTL_S = 25 * 24 * 3600    # WhatsApp keeps uploaded media for 30 days
_media_ids: dict[str, dict] = {}


def _load_media_cache() -> None:
    if _media_ids or not MEDIA_CACHE.exists():
        return
    try:
        _media_ids.update(json.loads(MEDIA_CACHE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


async def media_id_for(path: Path) -> str:
    """Upload a local photo to WhatsApp once and reuse its media id.

    Uploading beats sending a link: Meta would have to fetch the photo from our
    ngrok tunnel, which is slow and shows a warning page to unknown visitors.
    Ids are cached on disk (backend/.media_cache.json) and renewed before the
    30 days WhatsApp keeps media.
    """
    _load_media_cache()
    key = path.name
    cached = _media_ids.get(key)
    mtime = path.stat().st_mtime
    if cached and cached.get("mtime") == mtime and time.time() - cached.get("at", 0) < MEDIA_TTL_S:
        return cached["id"]

    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {os.environ['WHATSAPP_ACCESS_TOKEN']}"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url, headers=headers,
            data={"messaging_product": "whatsapp", "type": "image/jpeg"},
            files={"file": (path.name, path.read_bytes(), "image/jpeg")},
        )
    response.raise_for_status()
    media_id = response.json()["id"]
    _media_ids[key] = {"id": media_id, "mtime": mtime, "at": time.time()}
    try:
        MEDIA_CACHE.write_text(json.dumps(_media_ids, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"[whatsapp] could not save media cache: {exc!r}")
    print(f"[whatsapp] uploaded {path.name} as media {media_id}")
    return media_id


async def send_image(to: str, path: Path, caption: str = "") -> dict:
    """Send a product photo with a caption (WhatsApp formatting works in captions)."""
    media_id = await media_id_for(path)
    return await _post(to, {"type": "image", "image": {"id": media_id, "caption": caption[:MAX_CAPTION]}})


async def send_product_card(to: str, path: Path, caption: str, buttons: list[tuple[str, str]]) -> dict:
    """A product photo with up to three reply buttons under it.

    buttons: (id, title) pairs. When tapped, Meta sends an "interactive"
    message whose button_reply.id is that id (e.g. "hide:EW020"). Titles are
    capped at 20 characters and ids at 256 by WhatsApp.
    """
    media_id = await media_id_for(path)
    return await _post(to, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header": {"type": "image", "image": {"id": media_id}},
            "body": {"text": caption[:MAX_CAPTION]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": button_id[:256], "title": title[:MAX_CTA_BUTTON]}}
                for button_id, title in buttons[:3]
            ]},
        },
    })
