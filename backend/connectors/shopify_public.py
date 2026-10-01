"""Read a Shopify store's catalogue from its public /products.json.

Needs nothing from the owner: most Shopify shops serve this file openly. It
is unofficial (not the Admin API), some shops block it, and it has no stock
counts, only whether each variant is available. P3's installed app replaces
it with the Admin API and webhooks; until then it gives P2 real T-shirts.

Mapping (plan.md, P1 decisions):
  * one Shopify product in three colours -> three products here, one per
    colour, each with its own photo and embedding; sizes are the variants
  * external_id "<shopify product id>:<colour>", the variant's own id kept
    for P3's cart ("Default Title" = a product sold in one size)
  * codes are allocated by catalog_sync from the store's prefix (FW001...)

Politeness: at most one request per second, 250 products a page, and a 429
or 5xx is retried after Retry-After (or a growing pause), five times at most.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Callable, Optional

import httpx

from connectors.base import CommerceConnector

PAGE_SIZE = 250
MAX_PAGES = 100                 # 25,000 products: far beyond any pilot shop
MIN_INTERVAL_S = 1.0
MAX_TRIES = 5
USER_AGENT = "MunimCatalogImport/1.0 (WhatsApp shopping assistant; catalogue sync)"

_COLOUR_NAMES = {"color", "colour", "shade", "colors", "colours"}
_SIZE_NAMES = {"size", "sizes"}
_WOMEN = re.compile(r"\b(women|womens|women's|woman|ladies|female|girls?)\b", re.I)
_MEN = re.compile(r"\b(men|mens|men's|man|male|boys?|gents)\b", re.I)

Fetch = Callable[[str, dict[str, Any]], httpx.Response]


def _plain(body_html: Optional[str]) -> str:
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", ". ", body_html or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s*\.\s*(\.\s*)+", ". ", text)
    return re.sub(r"\s+", " ", text).strip(" .")[:1000]


def _tags(product: dict[str, Any]) -> list[str]:
    tags = product.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    return [t.strip() for t in tags if t.strip()]


def _gender(product: dict[str, Any]) -> str:
    words = " ".join([product.get("title", ""), product.get("product_type", ""), *_tags(product)])
    women, men = bool(_WOMEN.search(words)), bool(_MEN.search(words))
    return "Women" if women and not men else "Men" if men and not women else ""


def _option_index(product: dict[str, Any], names: set[str]) -> Optional[int]:
    for i, option in enumerate(product.get("options") or []):
        if str(option.get("name", "")).strip().lower() in names:
            return i
    return None


def _image_for(product: dict[str, Any], variants: list[dict[str, Any]]) -> Optional[str]:
    """The colour's own photo when Shopify links one, else the product's first."""
    for variant in variants:
        featured = variant.get("featured_image") or {}
        if featured.get("src"):
            return featured["src"]
    ids = {v.get("id") for v in variants}
    for image in product.get("images") or []:
        if ids & set(image.get("variant_ids") or []):
            return image.get("src")
    images = product.get("images") or []
    return images[0].get("src") if images else None


def to_records(product: dict[str, Any], shop_url: str) -> list[dict[str, Any]]:
    """One Shopify product -> one catalogue record per colour."""
    variants = product.get("variants") or []
    if not variants:
        return []
    colour_at = _option_index(product, _COLOUR_NAMES)
    size_at = _option_index(product, _SIZE_NAMES)
    groups: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        colour = variant.get(f"option{colour_at + 1}") if colour_at is not None else ""
        groups.setdefault(colour or "", []).append(variant)

    records = []
    for colour, group in groups.items():
        prices = [float(v.get("price") or 0) for v in group]
        compare = [float(v["compare_at_price"]) for v in group if v.get("compare_at_price")]
        price = min(prices)
        sized = []
        for variant in group:
            if size_at is not None:
                size = variant.get(f"option{size_at + 1}") or ""
            else:
                # No size option: other options (e.g. "Pack of 2") name the variant
                others = [variant.get(f"option{i}") for i in (1, 2, 3)
                          if variant.get(f"option{i}") and i - 1 != colour_at]
                size = " / ".join(o for o in others if o != "Default Title")
            sized.append({
                "size": size or "Free Size",
                "color": colour,
                "price": float(variant.get("price") or price),
                "mrp": float(variant["compare_at_price"]) if variant.get("compare_at_price") else None,
                "stock": None,                       # /products.json has no counts
                "available": bool(variant.get("available", True)),
                "external_id": str(variant.get("id")),
                "sku": variant.get("sku") or None,
                "image_url": ((variant.get("featured_image") or {}).get("src")),
            })
        title = product.get("title") or "Untitled"
        name = f"{title} - {colour}" if colour and len(groups) > 1 else title
        records.append({
            "external_id": f"{product.get('id')}:{colour}" if colour else str(product.get("id")),
            "name": name,
            "title": name,
            "brand": product.get("vendor") or "",
            "category": product.get("product_type") or "",
            "gender": _gender(product),
            "color": colour,
            "description": _plain(product.get("body_html")),
            "tags": _tags(product),
            "price": price,
            "mrp": max(compare) if compare and max(compare) > price else price,
            "image_url": _image_for(product, group),
            "url": f"{shop_url}/products/{product.get('handle')}",
            "in_stock": any(v["available"] for v in sized),
            "variants": sized,
        })
    return records


class ShopifyPublicConnector(CommerceConnector):
    platform = "shopify"

    def __init__(self, store: dict[str, Any], fetch: Optional[Fetch] = None,
                 sleep: Callable[[float], None] = time.sleep):
        super().__init__(store)
        domain = re.sub(r"^https?://", "", str(self.config.get("shop_domain") or "")).strip("/")
        if not domain:
            raise ValueError(f"store {store.get('slug')} has no shop_domain in its connector settings")
        self.shop_url = f"https://{domain}"
        self._fetch = fetch or self._http_get
        self._sleep = sleep
        self.requests = 0

    @staticmethod
    def _http_get(url: str, params: dict[str, Any]) -> httpx.Response:
        return httpx.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                         timeout=20.0, follow_redirects=True)

    def _page(self, page: int) -> list[dict[str, Any]]:
        for attempt in range(1, MAX_TRIES + 1):
            if self.requests:
                self._sleep(MIN_INTERVAL_S)
            self.requests += 1
            response = self._fetch(f"{self.shop_url}/products.json", {"limit": PAGE_SIZE, "page": page})
            if response.status_code == 429 or response.status_code >= 500:
                wait = float(response.headers.get("Retry-After") or 2 * attempt)
                print(f"[shopify] {response.status_code} on page {page}, waiting {wait:.0f}s "
                      f"(try {attempt}/{MAX_TRIES})")
                self._sleep(wait)
                continue
            response.raise_for_status()
            return response.json().get("products") or []
        raise RuntimeError(f"{self.shop_url}/products.json kept refusing (page {page})")

    def fetch_catalog(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            products = self._page(page)
            for product in products:
                records.extend(to_records(product, self.shop_url))
            if len(products) < PAGE_SIZE:
                break
        return records
