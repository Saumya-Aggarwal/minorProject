"""One Jinja environment for every page, with the helpers the templates share.

Each router used to build its own Jinja2Templates. One instance means the
filters and globals below exist on every page, so a partial (the product card,
the header) behaves the same wherever it is included.
"""

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi.templating import Jinja2Templates

import catalog

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def inr(value) -> str:
    """₹2,499 — Indian rupee formatting for prices shown to customers."""
    try:
        return f"₹{float(value):,.0f}"
    except (TypeError, ValueError):
        return "₹0"


def whatsapp_url(text: Optional[str] = None) -> Optional[str]:
    """wa.me link to our number, optionally with a pre-filled message."""
    number = os.getenv("WHATSAPP_DISPLAY_NUMBER", "").lstrip("+").replace(" ", "")
    if not number:
        return None
    return f"https://wa.me/{number}" + (f"?text={quote(text)}" if text else "")


# Main navigation: label -> listing URL. Kept here so header and footer agree.
NAV = [
    ("Men", "/shop?gender=Men"),
    ("Women", "/shop?gender=Women"),
    ("Sarees", "/shop?category=Saree"),
    ("Wedding Edit", "/shop?occasion=wedding"),
    ("Footwear", "/shop?category=Footwear"),
]

STATIC = Path(__file__).resolve().parent / "static"


def side_image() -> str:
    """Photo beside the sign-in forms: a dedicated one if present, else a product's."""
    for name in ("side.jpg", "side.webp"):
        if (STATIC / "hero" / name).exists():
            return f"/static/hero/{name}"
    product = catalog.get_by_id("EW017") or {}
    return product.get("image_url", "")


templates.env.filters["inr"] = inr
templates.env.globals["side_image"] = side_image
templates.env.globals["whatsapp_url"] = whatsapp_url
templates.env.globals["NAV"] = NAV


def asset_version() -> str:
    """Changes whenever site.css is rebuilt, so browsers never keep a stale copy."""
    try:
        return str(int((STATIC / "css" / "site.css").stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = asset_version
