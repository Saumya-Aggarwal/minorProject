"""A second invented brand's catalogue, built from photos chosen by eye.

Vyom Active is an invented activewear label (the demo is ethnic wear), so the
multi-brand bot can be shown with two very different shops. As with the demo
(fetch_photos.py), every product is written AFTER its photo was chosen, so the
text, the colour and the picture agree:

  1. candidates  search Pexels once per kind of garment and draw numbered
                 contact sheets into --work, for a person to look at (what
                 was found is kept in data/brands/vyom_candidates.json)
  2. build       for each pick in data/brands/vyom_picks.json (group, number,
                 what the photo shows), download the photo, crop it to 4:5,
                 save it in backend/static/brands/vyom/, and write the
                 catalogue to data/brands/vyom.json in the demo feed's shape

Then:  scripts/stores.py create vyom "Vyom Active" --platform feed \
           --prefix VA --feed data/brands/vyom.json
       scripts/sync_catalog.py vyom

Prices, ratings and stock are invented, from a fixed seed so a rebuild gives
the same catalogue. Pexels licence: free to use; each product keeps its credit.
"""

import argparse
import io
import json
import os
import random
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")
sys.stdout.reconfigure(encoding="utf-8")

PICKS = ROOT / "data" / "brands" / "vyom_picks.json"
# What candidates found (Pexels ids and URLs), kept so build can be rerun
FOUND = ROOT / "data" / "brands" / "vyom_candidates.json"
CATALOGUE = ROOT / "data" / "brands" / "vyom.json"
PHOTOS = ROOT / "backend" / "static" / "brands" / "vyom"
API = "https://api.pexels.com/v1/search"
SIZE = (900, 1125)
PER_PAGE = 30

GROUPS = {
    "m_tee": "man gym t-shirt",
    "m_over": "man oversized t-shirt streetwear",
    "m_tank": "man tank top workout",
    "m_jog": "man joggers sweatpants",
    "m_short": "man running shorts",
    "m_hood": "man hoodie",
    "m_jacket": "man track jacket",
    "w_bra": "woman sports bra",
    "w_leg": "woman leggings gym",
    "w_crop": "woman crop top workout",
    "w_tee": "woman t-shirt gym",
    "w_jog": "woman joggers",
    "w_short": "woman running shorts",
    "w_hood": "woman hoodie sporty",
    "bag": "gym duffel bag",
    "cap": "baseball cap sport",
}


def _font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def candidates(work: Path, only: list[str]) -> None:
    key = os.getenv("PEXELS_API_KEY") or sys.exit("PEXELS_API_KEY is not set in backend/.env")
    work.mkdir(parents=True, exist_ok=True)
    found_path = FOUND
    found = json.loads(found_path.read_text(encoding="utf-8")) if found_path.exists() else {}
    with httpx.Client(timeout=30) as client:
        for group, query in GROUPS.items():
            if only and group not in only:
                continue
            photos = client.get(API, params={"query": query, "per_page": PER_PAGE, "orientation": "portrait"},
                                headers={"Authorization": key}).raise_for_status().json().get("photos", [])
            found[group] = [{"id": p["id"], "photographer": p["photographer"], "url": p["url"],
                             "alt": p.get("alt", ""), "src": p["src"]["original"]} for p in photos]
            # Six across, numbered from 1, the group's name on top
            cols, w, h = 6, 160, 200
            rows = (len(photos) + cols - 1) // cols
            sheet = Image.new("RGB", (12 + cols * (w + 8), 40 + rows * (h + 26)), "white")
            draw = ImageDraw.Draw(sheet)
            draw.text((12, 10), f"{group}  —  {query}", fill="black", font=_font(18))
            for i, photo in enumerate(photos):
                raw = client.get(photo["src"]["medium"]).content
                thumb = ImageOps.fit(Image.open(io.BytesIO(raw)).convert("RGB"), (w, h))
                x, y = 12 + (i % cols) * (w + 8), 40 + (i // cols) * (h + 26)
                sheet.paste(thumb, (x, y))
                draw.text((x + 2, y + h + 3), str(i + 1), fill="black", font=_font(16))
            sheet.save(work / f"{group}.jpg", quality=80)
            print(f"{group:<9} {len(photos)} photos -> {group}.jpg")
    found_path.write_text(json.dumps(found, indent=1), encoding="utf-8")


# What each kind of garment is, for the records: category, sizes, price range,
# fabric, care and the sentences that describe it
KINDS = {
    "tee": {"category": "T-Shirts", "price": (599, 1299), "fabric": "Cotton-poly jersey",
            "pattern": "Solid", "sleeve": "Short sleeve", "neck": "Crew neck",
            "occasion": ["Gym", "Training", "Everyday"], "care": "Machine wash cold, do not tumble dry"},
    "oversized": {"category": "T-Shirts", "price": (799, 1499), "fabric": "Heavyweight cotton",
                  "pattern": "Solid", "sleeve": "Drop-shoulder short sleeve", "neck": "Crew neck",
                  "occasion": ["Everyday", "Gym", "Streetwear"], "care": "Machine wash cold, dry inside out"},
    "tank": {"category": "Tank Tops", "price": (499, 999), "fabric": "Quick-dry polyester mesh",
             "pattern": "Solid", "sleeve": "Sleeveless", "neck": "Scoop neck",
             "occasion": ["Gym", "Training", "Running"], "care": "Machine wash cold"},
    "joggers": {"category": "Joggers", "price": (1199, 2199), "fabric": "Brushed cotton fleece",
                "pattern": "Solid", "sleeve": "", "neck": "",
                "occasion": ["Everyday", "Travel", "Gym"], "care": "Machine wash cold, do not iron the print"},
    "shorts": {"category": "Shorts", "price": (699, 1299), "fabric": "Four-way stretch nylon",
               "pattern": "Solid", "sleeve": "", "neck": "",
               "occasion": ["Running", "Gym", "Training"], "care": "Machine wash cold, hang dry"},
    "hoodie": {"category": "Hoodies", "price": (1499, 2799), "fabric": "Cotton-rich fleece",
               "pattern": "Solid", "sleeve": "Full sleeve", "neck": "Hooded",
               "occasion": ["Everyday", "Travel", "Winter"], "care": "Machine wash cold, dry flat"},
    "jacket": {"category": "Jackets", "price": (1799, 3299), "fabric": "Water-resistant polyester",
               "pattern": "Solid", "sleeve": "Full sleeve", "neck": "Stand collar",
               "occasion": ["Running", "Travel", "Winter"], "care": "Machine wash cold, do not bleach"},
    "bra": {"category": "Sports Bras", "price": (799, 1599), "fabric": "Nylon-spandex with removable pads",
            "pattern": "Solid", "sleeve": "Sleeveless", "neck": "Scoop neck",
            "occasion": ["Gym", "Yoga", "Running"], "care": "Hand wash or gentle cycle in a laundry bag"},
    "leggings": {"category": "Leggings", "price": (999, 1899), "fabric": "Squat-proof nylon-spandex",
                 "pattern": "Solid", "sleeve": "", "neck": "",
                 "occasion": ["Yoga", "Gym", "Running"], "care": "Machine wash cold, no fabric softener"},
    "crop": {"category": "Crop Tops", "price": (599, 1199), "fabric": "Breathable polyester-spandex",
             "pattern": "Solid", "sleeve": "Short sleeve", "neck": "Crew neck",
             "occasion": ["Gym", "Yoga", "Everyday"], "care": "Machine wash cold"},
    "bag": {"category": "Bags", "price": (1299, 2499), "fabric": "Ripstop polyester",
            "pattern": "Solid", "sleeve": "", "neck": "",
            "occasion": ["Gym", "Travel"], "care": "Wipe clean with a damp cloth"},
    "cap": {"category": "Caps", "price": (499, 899), "fabric": "Cotton twill",
            "pattern": "Solid", "sleeve": "", "neck": "",
            "occasion": ["Running", "Everyday", "Outdoor"], "care": "Hand wash, air dry"},
}
FITS = {"tee": "Regular", "oversized": "Oversized", "tank": "Regular", "joggers": "Tapered", "shorts": "Regular",
        "hoodie": "Relaxed", "jacket": "Regular", "bra": "Medium support", "leggings": "High-waisted",
        "crop": "Cropped", "bag": "", "cap": ""}
LETTERS = {"Men": ["S", "M", "L", "XL", "XXL"], "Women": ["XS", "S", "M", "L", "XL"]}
HIGHLIGHTS = {
    "tee": ["Sweat-wicking jersey that dries fast between sets", "Flatlock seams that do not rub under a bag strap",
            "Holds its shape wash after wash"],
    "oversized": ["Dropped shoulders and a boxy body for an easy, relaxed drape", "220 GSM cotton that feels substantial",
                  "Ribbed neck that keeps its shape"],
    "tank": ["Open mesh back panel for airflow", "Deep armholes for full range of motion", "Lightweight and quick-drying"],
    "joggers": ["Elastic waist with an inner drawcord", "Zip pockets that keep a phone in place",
                "Ribbed cuffs that sit neatly above trainers"],
    "shorts": ["Built-in liner and a back zip pocket", "Split hem for a longer stride", "Reflective logo for early runs"],
    "hoodie": ["Double-layer hood with flat drawcords", "Kangaroo pocket", "Soft brushed inside"],
    "jacket": ["Full zip with a chin guard", "Light water-resistant finish for drizzle", "Zip hand pockets"],
    "bra": ["Medium support for gym, yoga and runs", "Removable pads", "Wide band that does not roll"],
    "leggings": ["High waistband that stays put through squats", "Squat-proof, non-sheer fabric",
                 "Side pocket that fits a phone"],
    "crop": ["Cropped to sit just above high-waisted leggings", "Stretchy and breathable", "Soft, tag-free neck"],
    "bag": ["Separate shoe compartment", "Water-resistant base", "Padded adjustable shoulder strap"],
    "cap": ["Adjustable back strap", "Curved peak against the sun", "Sweatband inside"],
}


def _sizes(kind: str, gender: str) -> list[str]:
    if kind in ("bag", "cap"):
        return ["Free Size"]
    return LETTERS["Women" if gender == "Women" else "Men"]


def build() -> None:
    picks = json.loads(PICKS.read_text(encoding="utf-8"))
    found = json.loads(FOUND.read_text(encoding="utf-8"))
    rng = random.Random(20261102)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    products = []
    with httpx.Client(timeout=60) as client:
        for n, pick in enumerate(picks["products"], 1):
            code = f"VA{n:03d}"
            photo = found[pick["group"]][pick["n"] - 1]
            target = PHOTOS / f"{code}.jpg"
            if not target.exists():
                raw = client.get(photo["src"], params={"auto": "compress", "cs": "tinysrgb", "w": 1400}).content
                image = ImageOps.fit(Image.open(io.BytesIO(raw)).convert("RGB"), SIZE, Image.LANCZOS,
                                     centering=(0.5, pick.get("focus_y", 0.4)))
                image.save(target, "JPEG", quality=82, optimize=True, progressive=True)
            kind = KINDS[pick["kind"]]
            gender = pick["gender"]
            low, high = kind["price"]
            price = rng.randrange(low, high + 1, 100) - 1 if high - low >= 100 else low
            mrp = int(round(price * rng.uniform(1.25, 1.6), -2)) - 1
            sizes = _sizes(pick["kind"], gender)
            stock = {s: rng.choice([0, 3, 6, 9, 12, 18]) if len(sizes) > 1 else rng.choice([8, 15, 22]) for s in sizes}
            if not any(stock.values()):
                stock[sizes[len(sizes) // 2]] = 7
            who = {"Men": "Men's", "Women": "Women's"}.get(gender, "Unisex")
            fit = pick.get("fit") or FITS[pick["kind"]]
            colour = pick["colour"]
            name = pick["name"]
            occasions = pick.get("occasion") or kind["occasion"]
            products.append({
                "id": code,
                "sku": f"VYM-{kind['category'][:3].upper()}-{n:03d}",
                "name": name,
                "title": f"Vyom Active {who} {colour} {name} - {', '.join(occasions[:2])}",
                "brand": "Vyom Active",
                "gender": gender,
                "category": kind["category"],
                "subcategory": pick.get("subcategory") or kind["category"],
                "fabric": pick.get("fabric") or kind["fabric"],
                "color": colour,
                "pattern": pick.get("pattern") or kind["pattern"],
                "occasion": occasions,
                "fit": fit,
                "sleeve": pick.get("sleeve") or kind["sleeve"],
                "neck": kind["neck"],
                "price": price,
                "mrp": max(mrp, price + 100),
                "discount_percent": round(100 * (1 - price / max(mrp, price + 100))),
                "currency": "INR",
                "rating": round(rng.uniform(3.9, 4.7), 1),
                "rating_count": rng.randrange(40, 2400),
                "sizes_available": sizes,
                "stock": stock,
                "in_stock": any(stock.values()),
                "highlights": [pick["look"]] + HIGHLIGHTS[pick["kind"]],
                "description": f"{pick['look']}. {pick.get('more') or ''}".strip(),
                "care": kind["care"],
                "country_of_origin": "India",
                "seller": "Vyom Active Retail LLP",
                "delivery_days": rng.choice([2, 3, 4]),
                "return_days": 15,
                "image_url": f"/static/brands/vyom/{code}.jpg",
                "image_credit": {"photographer": photo["photographer"], "url": photo["url"]},
            })
            print(f"{code} {gender:<6} {kind['category']:<12} {colour:<14} {name}")
    CATALOGUE.write_text(json.dumps(products, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(products)} products -> {CATALOGUE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--work", required=True, type=Path)
    c.add_argument("--only", nargs="*", default=[])
    sub.add_parser("build")
    args = parser.parse_args()
    if args.step == "candidates":
        candidates(args.work, args.only)
    else:
        build()


if __name__ == "__main__":
    main()
