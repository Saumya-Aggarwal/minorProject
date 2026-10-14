"""Write evals/catalogues/fitwear.json: an invented activewear shop, "Stride Lab",
in Shopify's /products.json shape (the brand and products are made up).

It stands in for the pilot's T-shirt and workout-clothes store until his real
catalogue is imported: scripts/test_brands.py builds a second store from it to
prove the bot speaks for that brand and understands its words. Run again after
editing PRODUCTS; the output is deterministic.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evals" / "catalogues" / "fitwear.json"

ADULT = ["S", "M", "L", "XL", "XXL"]
WOMEN = ["XS", "S", "M", "L", "XL"]

# title, product_type, tags, colours, sizes, price, compare_at, description
PRODUCTS = [
    ("Oversized Graphic Tee", "T-Shirts", "oversized, cotton, streetwear, Unisex", ["Black", "White", "Sage"],
     ADULT, 799, 999, "Heavyweight 240 GSM cotton tee with a dropped shoulder and a small chest print."),
    ("Dri-Fit Training Tee", "T-Shirts", "gym, quick-dry, Men", ["Navy", "Grey", "Black"],
     ADULT, 899, None, "Quick-dry polyester mesh for the gym and running. Flatlock seams, no chafing."),
    ("Essential Crew Tee", "T-Shirts", "cotton, everyday, Men", ["White", "Black", "Olive"],
     ADULT, 599, 699, "A plain regular-fit crew neck in soft combed cotton for every day."),
    ("Cropped Workout Tee", "T-Shirts", "gym, yoga, Women", ["Lilac", "Black"],
     WOMEN, 749, None, "A cropped, relaxed tee in a breathable modal blend for training and yoga."),
    ("Seamless Sports Bra", "Sports Bras", "yoga, gym, Women", ["Black", "Dusty Pink"],
     ["XS", "S", "M", "L"], 999, 1299, "Medium support seamless bra with removable pads, for yoga and gym days."),
    ("High-Impact Sports Bra", "Sports Bras", "running, Women", ["Black", "Navy"],
     ["XS", "S", "M", "L"], 1299, None, "High support for running and HIIT, with adjustable straps and a hook back."),
    ("Tapered Joggers", "Joggers", "running, winter, Men", ["Charcoal", "Black"],
     ADULT, 1499, 1799, "Brushed fleece joggers with a tapered leg, zip pockets and ribbed cuffs."),
    ("Relaxed Joggers", "Joggers", "yoga, lounge, Women", ["Grey Melange", "Black"],
     WOMEN, 1399, None, "Soft French terry joggers with a wide waistband for yoga, travel and lounging."),
    ("2-in-1 Running Shorts", "Shorts", "running, quick-dry, Men", ["Black", "Navy"],
     ADULT, 999, None, "Lightweight running shorts with a built-in liner and a phone pocket."),
    ("Biker Shorts", "Shorts", "gym, cycling, Women", ["Black", "Olive"],
     WOMEN, 899, 1099, "High-waist biker shorts in a squat-proof stretch fabric."),
    ("High-Waist Leggings", "Leggings", "yoga, gym, squat-proof, Women", ["Black", "Wine"],
     WOMEN, 1599, 1999, "Full-length leggings with a high waistband and a hidden pocket, squat-proof."),
    ("Muscle Tank", "Tank Tops", "gym, bodybuilding, Men", ["Black", "White", "Red"],
     ADULT, 649, None, "A deep-cut muscle tank in breathable cotton for heavy lifting days."),
    ("Racerback Tank", "Tank Tops", "yoga, Women", ["White", "Teal"],
     WOMEN, 699, None, "A light racerback tank that stays out of the way in yoga and pilates."),
    ("Zip Hoodie", "Hoodies", "winter, cotton, Unisex", ["Black", "Grey"],
     ADULT, 1999, 2499, "A mid-weight cotton zip hoodie for warm-ups and cold mornings."),
    ("Windbreaker Jacket", "Jackets", "running, rain, Unisex", ["Neon Yellow", "Black"],
     ADULT, 2499, None, "A packable, water-resistant windbreaker with reflective trims for evening runs."),
    ("Gym Duffel Bag", "Accessories", "gym", ["Black"], None, 1799, None,
     "A 35-litre duffel with a shoe compartment and a wet pocket."),
    ("Ankle Socks (3 Pack)", "Accessories", "running, Unisex", ["White"], None, 349, None,
     "Cushioned cotton ankle socks, three pairs."),
    ("Training Cap", "Accessories", "running, Unisex", ["Black"], None, 499, None,
     "A light, quick-dry cap with an adjustable back."),
]


def product(n: int, title: str, kind: str, tags: str, colours: list[str], sizes, price: int,
            compare, description: str) -> dict:
    pid = 8000 + n
    handle = title.lower().replace(" ", "-").replace("(", "").replace(")", "")
    if sizes is None:
        options = [{"name": "Title", "position": 1, "values": ["Default Title"]}]
        variants = [{"id": pid * 100 + 1, "title": "Default Title", "option1": "Default Title", "option2": None,
                     "option3": None, "price": f"{price}.00",
                     "compare_at_price": f"{compare}.00" if compare else None, "available": True,
                     "sku": f"SL-{pid}"}]
    else:
        options = [{"name": "Color", "position": 1, "values": colours},
                   {"name": "Size", "position": 2, "values": sizes}]
        variants = []
        for c, colour in enumerate(colours):
            for s, size in enumerate(sizes):
                variants.append({
                    "id": pid * 100 + c * 10 + s, "title": f"{colour} / {size}", "option1": colour,
                    "option2": size, "option3": None, "price": f"{price}.00",
                    "compare_at_price": f"{compare}.00" if compare else None,
                    # A few sizes sold out, as in a real shop
                    "available": not (size == "XXL" and c == 0 and n % 3 == 0),
                    "sku": f"SL-{pid}-{colour[:3].upper()}-{size}"})
    return {"id": pid, "title": title, "handle": handle, "body_html": f"<p>{description}</p>",
            "vendor": "Stride Lab", "product_type": kind, "tags": tags, "options": options,
            "variants": variants, "images": [{"src": f"https://cdn.example.com/stride/{handle}.jpg",
                                               "variant_ids": []}]}


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    data = {"products": [product(n, *row) for n, row in enumerate(PRODUCTS, 1)]}
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(PRODUCTS)} products to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
