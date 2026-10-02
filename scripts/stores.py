"""Manage stores from the command line (the brand dashboard arrives in P4).

Run from the repo root, e.g.:
    backend/.venv/Scripts/python scripts/stores.py list
    backend/.venv/Scripts/python scripts/stores.py create fitwear "FitWear" --platform shopify \
        --prefix FW --shop fitwear.myshopify.com
    backend/.venv/Scripts/python scripts/stores.py set-whatsapp fitwear --phone-number-id 1234 \
        --display-number 919800000000 --waba-id 5678 --app-id 9012
    backend/.venv/Scripts/python scripts/stores.py status fitwear live
    backend/.venv/Scripts/python scripts/stores.py rotate-keys

set-whatsapp asks for the access token, app secret and verify token at a
hidden prompt, so they never land in shell history; they are stored encrypted
(backend/secrets_box.py, key in MUNIM_SECRET_KEYS).
"""

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

from sqlmodel import select  # noqa: E402

import secrets_box  # noqa: E402
import tenancy  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from models import Product, Store, utcnow  # noqa: E402

SECRET_COLUMNS = ("wa_access_token_enc", "wa_app_secret_enc", "wa_verify_token_enc")


def _store(db, slug: str) -> Store:
    store = db.exec(select(Store).where(Store.slug == slug)).first()
    if store is None:
        sys.exit(f"No store with slug {slug!r}")
    return store


def cmd_list(_args) -> None:
    with session_scope() as db:
        for s in db.exec(select(Store).order_by(Store.store_id)).all():
            count = len(db.exec(select(Product.product_pk).where(Product.store_id == s.store_id,
                                                                 Product.active)).all())
            whatsapp = s.wa_phone_number_id or ("backend/.env" if s.store_id == tenancy.DEMO_STORE_ID else "-")
            print(f"#{s.store_id:<3} {s.slug:<16} {s.status:<7} {s.platform:<8} {count:>4} products  "
                  f"WhatsApp: {whatsapp}  token: {'set' if s.wa_access_token_enc else '-'}")


def cmd_create(args) -> None:
    connector = {"shop_domain": args.shop} if args.shop else {"feed_path": args.feed} if args.feed else None
    with session_scope() as db:
        store = Store(slug=args.slug, name=args.name, platform=args.platform, code_prefix=args.prefix.upper(),
                      connector=connector, status="draft")
        db.add(store)
        db.flush()
        print(f"Created store #{store.store_id} {args.slug} (draft)")
    tenancy.forget()


def cmd_set_whatsapp(args) -> None:
    secrets = {
        "wa_access_token_enc": getpass.getpass("Access token (permanent system-user token): "),
        "wa_app_secret_enc": getpass.getpass("App secret (Meta App Dashboard > Settings > Basic; without it messages are refused): "),
        "wa_verify_token_enc": getpass.getpass("Webhook verify token (blank to skip): "),
    }
    with session_scope() as db:
        store = _store(db, args.slug)
        store.wa_phone_number_id = args.phone_number_id
        store.wa_display_number = args.display_number
        store.wa_business_account_id = args.waba_id or store.wa_business_account_id
        store.wa_app_id = args.app_id or store.wa_app_id
        for column, value in secrets.items():
            if value.strip():
                setattr(store, column, secrets_box.encrypt(value.strip()))
        store.updated_at = utcnow()
    tenancy.forget()
    print(f"WhatsApp settings saved for {args.slug} (secrets encrypted)")


def cmd_status(args) -> None:
    with session_scope() as db:
        store = _store(db, args.slug)
        store.status = args.status
        store.updated_at = utcnow()
    tenancy.forget()
    print(f"{args.slug} is now {args.status}")


def cmd_rotate_keys(_args) -> None:
    """Re-encrypt every stored secret with the first key in MUNIM_SECRET_KEYS."""
    rotated = 0
    with session_scope() as db:
        for store in db.exec(select(Store)).all():
            for column in SECRET_COLUMNS:
                if getattr(store, column):
                    setattr(store, column, secrets_box.rotate(getattr(store, column)))
                    rotated += 1
    print(f"Re-encrypted {rotated} secrets with the newest key. The old key can now be removed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").set_defaults(run=cmd_list)

    create = sub.add_parser("create")
    create.add_argument("slug")
    create.add_argument("name")
    create.add_argument("--platform", default="shopify", choices=["feed", "shopify", "woo", "api"])
    create.add_argument("--prefix", required=True, help="product code prefix, e.g. FW -> FW001")
    create.add_argument("--shop", help="Shopify domain, e.g. brand.myshopify.com or brand.com")
    create.add_argument("--feed", help="feed file, relative to the repo root")
    create.set_defaults(run=cmd_create)

    whatsapp = sub.add_parser("set-whatsapp")
    whatsapp.add_argument("slug")
    whatsapp.add_argument("--phone-number-id", required=True)
    whatsapp.add_argument("--display-number", required=True, help="digits only, no +")
    whatsapp.add_argument("--waba-id")
    whatsapp.add_argument("--app-id")
    whatsapp.set_defaults(run=cmd_set_whatsapp)

    status = sub.add_parser("status")
    status.add_argument("slug")
    status.add_argument("status", choices=["draft", "live", "paused"])
    status.set_defaults(run=cmd_status)

    sub.add_parser("rotate-keys").set_defaults(run=cmd_rotate_keys)

    args = parser.parse_args()
    init_db()
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
