"""Product photography from Pexels, chosen by a person, stored in the project.

Two steps, so a human picks every photo rather than taking the first result:

  1. candidates  search Pexels once per product, download thumbnails, and
                 build contact sheets (numbered grids) for review
  2. apply       for each choice in data/photo_choices.json, download the
                 photo, crop it to the shop's 4:5 format, save it into
                 backend/static/products/, and record the credit in
                 data/products.json

Photos are stored locally so the shop renders with no internet at the venue.
Pexels licence: free to use, no attribution required; the API guidelines ask
for a visible credit, which the footer and product pages give.

Usage (from the repo root, PEXELS_API_KEY in backend/.env):
  backend/.venv/Scripts/python scripts/fetch_photos.py candidates --work <dir>
  backend/.venv/Scripts/python scripts/fetch_photos.py apply
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "backend" / ".env")

PRODUCTS = ROOT / "data" / "products.json"
CHOICES = ROOT / "data" / "photo_choices.json"
OUT = ROOT / "backend" / "static" / "products"
HERO = ROOT / "backend" / "static" / "hero"

API = "https://api.pexels.com/v1/search"
SIZE = (900, 1125)  # 4:5, matches every product frame on the site

# One query per product, written by hand: the garment and colour first, and
# "man"/"woman" so results show the piece being worn, as a shop would.
QUERIES = {
    "EW001": "man maroon kurta", "EW002": "man blue kurta", "EW003": "man white kurta",
    "EW004": "man nehru jacket", "EW005": "man printed nehru jacket",
    "EW006": "groom blue sherwani", "EW007": "groom gold sherwani",
    "EW008": "man black bandhgala", "EW009": "man pathani suit",
    "EW010": "man ethnic waistcoat", "EW011": "man yellow kurta",
    "EW012": "groom stole sherwani", "EW013": "man white churidar",
    "EW014": "man white dhoti", "EW015": "man black kurta", "EW016": "man green kurta",
    "EW017": "woman red anarkali", "EW018": "bride pink lehenga",
    "EW019": "woman blue lehenga", "EW020": "woman green silk saree",
    "EW021": "woman beige saree", "EW022": "woman black saree",
    "EW023": "woman white kurti", "EW024": "woman yellow kurti",
    "EW025": "woman peach salwar suit", "EW026": "woman sharara",
    "EW027": "woman block print kurta", "EW028": "woman maroon indian gown",
    "EW029": "banarasi dupatta", "EW030": "woman black palazzo",
    "EW031": "woman teal salwar kameez", "EW032": "woman green kurta",
    # Not products: the home page hero and the sign-in page photo
    "hero": "indian groom sherwani", "side": "indian bride",
    # Second collection (22 Sep): pieces for each function, footwear, accessories.
    # Product text is written after the photo is chosen, to match it.
    "EW033": "man yellow kurta haldi", "EW034": "man white kurta pajama",
    "EW035": "man mint green kurta", "EW036": "man printed kurta",
    "EW037": "man kurta with jacket", "EW038": "man black indo western",
    "EW039": "man navy sherwani", "EW040": "man dhoti kurta",
    "EW041": "groom maroon sherwani", "EW042": "groom pink sherwani",
    "EW043": "man grey bandhgala suit", "EW044": "man green nehru jacket",
    "EW045": "man orange kurta", "EW046": "man pathani eid",
    "EW047": "man green silk kurta", "EW048": "man saffron kurta",
    "EW049": "man linen kurta", "EW050": "man cotton kurta casual",
    "EW051": "groom safa turban", "EW052": "mojari shoes",
    "EW053": "kolhapuri chappal", "EW054": "groom dupatta stole",
    "EW055": "woman yellow sharara", "EW056": "woman yellow lehenga",
    "EW057": "woman green lehenga mehendi", "EW058": "woman chaniya choli",
    "EW059": "woman gharara", "EW060": "woman pink saree",
    "EW061": "woman sequin saree", "EW062": "woman indo western outfit",
    "EW063": "bride red lehenga", "EW064": "woman banarasi saree",
    "EW065": "woman golden saree", "EW066": "kerala saree woman",
    "EW067": "woman pastel anarkali", "EW068": "woman blue salwar suit",
    "EW069": "woman white anarkali", "EW070": "navratri garba dress",
    "EW071": "woman cotton saree", "EW072": "woman kurta set office",
    "EW073": "woman white kurta palazzo", "EW074": "woman teal lehenga",
    "EW075": "woman pink gown indian", "EW076": "woman mustard kurti",
    "EW077": "embroidered juttis", "EW078": "woman kolhapuri sandals",
    "EW079": "potli bag", "EW080": "embroidered clutch indian",
    "EW081": "phulkari dupatta", "EW082": "mirror work dupatta",
    "EW083": "men ethnic shoes wedding", "EW084": "woman ethnic sandals heels",
}


def _key() -> str:
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        sys.exit("PEXELS_API_KEY is not set in backend/.env")
    return key


def search(client: httpx.Client, query: str, per_page: int) -> list[dict]:
    response = client.get(
        API,
        params={"query": query, "per_page": per_page, "orientation": "portrait"},
        headers={"Authorization": _key()},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("photos", [])


def _font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def candidates(work: Path, per_page: int, only: list[str]) -> None:
    """Thumbnails and contact sheets for review; metadata in candidates.json."""
    work.mkdir(parents=True, exist_ok=True)
    products = {p["id"]: p for p in json.loads(PRODUCTS.read_text(encoding="utf-8"))}
    found: dict[str, list[dict]] = {}
    thumbs: dict[str, list[Image.Image]] = {}

    with httpx.Client() as client:
        for key, query in QUERIES.items():
            if only and key not in only:
                continue
            photos = search(client, query, per_page)
            found[key] = [
                {"id": p["id"], "photographer": p["photographer"], "url": p["url"],
                 "alt": p.get("alt", ""), "src": p["src"]["original"]}
                for p in photos
            ]
            thumbs[key] = []
            for photo in photos:
                raw = client.get(photo["src"]["medium"], timeout=30).content
                thumbs[key].append(ImageOps.fit(Image.open(io.BytesIO(raw)).convert("RGB"), (150, 188)))
            print(f"{key:<6} {len(photos)} results for {query!r}")

    (work / "candidates.json").write_text(json.dumps(found, indent=2), encoding="utf-8")

    # Contact sheets: three products per image, numbered 1..n under each thumbnail
    keys = list(thumbs)
    label_font, num_font = _font(15), _font(13)
    for sheet_no in range(0, len(keys), 3):
        group = keys[sheet_no:sheet_no + 3]
        width = 12 + per_page * 158
        sheet = Image.new("RGB", (width, len(group) * 238 + 10), "white")
        draw = ImageDraw.Draw(sheet)
        for row, key in enumerate(group):
            y = 10 + row * 238
            name = products[key]["name"] + f" ({products[key]['color']})" if key in products else key
            draw.text((12, y), f"{key}  {name}  —  query: {QUERIES[key]}", fill="black", font=label_font)
            for i, thumb in enumerate(thumbs[key]):
                x = 12 + i * 158
                sheet.paste(thumb, (x, y + 22))
                draw.text((x + 2, y + 212), str(i + 1), fill="black", font=num_font)
        path = work / f"sheet_{sheet_no // 3 + 1:02d}.jpg"
        sheet.save(path, quality=82)
        print("wrote", path)


def _compact(value, depth: int = 0) -> str:
    """JSON in the catalogue's hand-written layout: short flat lists and dicts
    (sizes, stock, occasion) stay on one line, everything else is indented."""
    pad, inner_pad = "  " * depth, "  " * (depth + 1)
    flat = isinstance(value, (list, dict)) and not any(
        isinstance(v, (list, dict)) for v in (value.values() if isinstance(value, dict) else value))
    one_line = json.dumps(value, ensure_ascii=False)
    if not isinstance(value, (list, dict)) or (flat and depth >= 2 and len(one_line) <= 80):
        return one_line
    if isinstance(value, dict):
        parts = [f"{inner_pad}{json.dumps(k)}: {_compact(v, depth + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    parts = [inner_pad + _compact(v, depth + 1) for v in value]
    return "[\n" + ",\n".join(parts) + "\n" + pad + "]"


def write_catalogue(products: list[dict]) -> None:
    PRODUCTS.write_text(_compact(products) + "\n", encoding="utf-8")


def apply(work: Path) -> None:
    """Download the chosen photos, crop to 4:5, save locally, update the catalogue."""
    found = json.loads((work / "candidates.json").read_text(encoding="utf-8"))
    choices = json.loads(CHOICES.read_text(encoding="utf-8"))
    products = json.loads(PRODUCTS.read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in products}
    OUT.mkdir(parents=True, exist_ok=True)
    HERO.mkdir(parents=True, exist_ok=True)

    with httpx.Client() as client:
        for key, choice in choices.items():
            if key.startswith("_"):
                continue
            photo = next((p for p in found.get(key, []) if p["id"] == choice["pexels_id"]), None)
            if photo is None:
                print(f"skip {key}: photo {choice['pexels_id']} not among its candidates")
                continue
            raw = client.get(photo["src"], params={"auto": "compress", "cs": "tinysrgb", "w": 1400},
                             timeout=60).content
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            # Crop to 4:5 around the chosen focus (0 = top, 0.5 = centre)
            focus = (0.5, choice.get("focus_y", 0.35))
            image = ImageOps.fit(image, SIZE, Image.LANCZOS, centering=focus)

            if key in ("hero", "side"):
                target, url = HERO / f"{key}.jpg", None
            else:
                target, url = OUT / f"{key}.jpg", f"/static/products/{key}.jpg"
            image.save(target, "JPEG", quality=84, optimize=True, progressive=True)
            print(f"{key:<6} {target.name}  {target.stat().st_size // 1024} KB  by {photo['photographer']}")

            if url and key in by_id:
                by_id[key]["image_url"] = url
                by_id[key]["image_credit"] = {"photographer": photo["photographer"], "url": photo["url"]}

    write_catalogue(products)
    print("catalogue updated:", PRODUCTS)


def main() -> None:
    # Photographers' names are not all ASCII; the Windows console default is cp1252
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["candidates", "apply"])
    parser.add_argument("--work", type=Path, default=ROOT / "data" / "photo_work")
    parser.add_argument("--per-page", type=int, default=8)
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    if args.step == "candidates":
        candidates(args.work, args.per_page, args.only)
    else:
        apply(args.work)


if __name__ == "__main__":
    main()
