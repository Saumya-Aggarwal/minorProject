"""Generate a placeholder image per product.

Locally generated SVG rather than stock photo URLs on purpose: an external image
host that is slow or blocked turns the storefront into a grid of broken icons,
and demo venues have unreliable wifi. These render offline, always.

Swap in real photographs later by replacing image_url in data/products.json.

Run: backend/.venv/Scripts/python scripts/generate_placeholders.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "backend" / "static" / "products"

# Colour per garment colour name, so the grid reads as a real catalogue
SWATCHES = {
    "Maroon": ("#6b1f2a", "#8f2f3d"),
    "Indigo": ("#2a3c66", "#3d5490"),
    "Ivory": ("#e8e0d2", "#d6ccb9"),
    "Teal": ("#1f5f63", "#2b8086"),
    "Royal blue": ("#1e3a8a", "#2c50b8"),
    "Champagne gold": ("#b08d4f", "#cfae72"),
    "Black": ("#26262a", "#3d3d44"),
    "Olive": ("#5a6236", "#78824a"),
    "Wine": ("#5c1f33", "#7d2b46"),
    "Mustard": ("#b4842a", "#d6a23f"),
    "Cream": ("#ece4d4", "#dbd0ba"),
    "White": ("#f2f2ef", "#dedede"),
    "Sage green": ("#7c8a6b", "#9aa889"),
    "Crimson": ("#8c1c2b", "#b0293c"),
    "Blush pink": ("#d9a6a8", "#e8c2c3"),
    "Navy blue": ("#1c2544", "#2b3866"),
    "Emerald green": ("#14584a", "#1d7c68"),
    "Beige": ("#cfc0a8", "#e0d4c0"),
    "Terracotta": ("#a85736", "#c2724f"),
    "Peach": ("#e6b49a", "#f0cbb8"),
    "Antique gold": ("#9c7c3c", "#bd9a54"),
}
DEFAULT = ("#4b4b52", "#6a6a73")


def escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def wrap(text: str, width: int = 18) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines[:3]


def svg_for(product: dict) -> str:
    dark, light = SWATCHES.get(product["color"], DEFAULT)
    lines = wrap(product["name"])
    start_y = 210 - (len(lines) - 1) * 16
    text = "".join(
        f'<text x="200" y="{start_y + i * 32}" text-anchor="middle" '
        f'font-family="Georgia, serif" font-size="25" fill="#ffffff" '
        f'opacity="0.95">{escape(line)}</text>'
        for i, line in enumerate(lines)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 500" width="400" height="500" role="img" aria-label="{escape(product['name'])}">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{light}"/>
      <stop offset="100%" stop-color="{dark}"/>
    </linearGradient>
  </defs>
  <rect width="400" height="500" fill="url(#g)"/>
  <rect x="24" y="24" width="352" height="452" fill="none" stroke="#ffffff" stroke-opacity="0.28"/>
  <text x="200" y="120" text-anchor="middle" font-family="Helvetica, Arial, sans-serif"
        font-size="13" letter-spacing="4" fill="#ffffff" opacity="0.75">{escape(product['brand'].upper())}</text>
  {text}
  <text x="200" y="400" text-anchor="middle" font-family="Helvetica, Arial, sans-serif"
        font-size="14" letter-spacing="2" fill="#ffffff" opacity="0.7">{escape(product['category'].upper())}</text>
  <text x="200" y="430" text-anchor="middle" font-family="Helvetica, Arial, sans-serif"
        font-size="13" fill="#ffffff" opacity="0.6">{escape(product['color'])}</text>
</svg>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    products = json.loads((ROOT / "data" / "products.json").read_text(encoding="utf-8"))

    for product in products:
        (OUT_DIR / f"{product['id']}.svg").write_text(svg_for(product), encoding="utf-8")

    print(f"Wrote {len(products)} placeholder images to {OUT_DIR}")


if __name__ == "__main__":
    main()
