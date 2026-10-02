"""Which store a piece of work belongs to.

`current store` is a ContextVar, so it follows a request through awaits,
asyncio.to_thread and the background task that answers a WhatsApp message,
without being passed through every function.

Unset means the demo store (#1): the website, the scripts and every test
written before stores existed run against it unchanged. The webhook sets it
from the phone number a message was sent to.

Rows that already know their customer take the store from the user row, never
from the context: an order or a log line can only ever belong to its user's
store, whatever the context says.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from sqlmodel import select

DEMO_STORE_ID = 1
CACHE_SECONDS = 30

_current: ContextVar[Optional[int]] = ContextVar("current_store", default=None)
_cache: dict[Any, tuple[float, Any]] = {}


def store_id() -> int:
    """The store the current work is for."""
    value = _current.get()
    return DEMO_STORE_ID if value is None else value


@contextmanager
def use_store(store: int) -> Iterator[int]:
    """Run a block for one store; the previous store is restored afterwards."""
    token = _current.set(store)
    try:
        yield store
    finally:
        _current.reset(token)


def _cached(key: Any, load) -> Any:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = load()
    _cache[key] = (time.time(), value)
    return value


def forget() -> None:
    """Drop cached store rows (after a store is created or edited)."""
    _cache.clear()


def get_store(store: Optional[int] = None) -> Optional[dict[str, Any]]:
    """The store row as a dict (the *_enc columns still encrypted)."""
    from db import session_scope
    from models import Store

    sid = store_id() if store is None else store

    def load() -> Optional[dict[str, Any]]:
        with session_scope() as db:
            row = db.get(Store, sid)
            return row.model_dump() if row else None

    return _cached(("store", sid), load)


def store_for_phone_number(phone_number_id: Optional[str]) -> Optional[int]:
    """The live store that owns this WhatsApp number, or None to ignore the message.

    The demo store's number is the one in backend/.env, and a payload without
    metadata (the local simulator, the tests) is for the demo store too.
    """
    from db import session_scope
    from models import Store

    if not phone_number_id:
        return DEMO_STORE_ID

    def load() -> Optional[int]:
        with session_scope() as db:
            row = db.exec(select(Store).where(Store.wa_phone_number_id == phone_number_id)).first()
            if row is not None:
                return row.store_id if row.status == "live" else None
        return DEMO_STORE_ID if phone_number_id == os.getenv("WHATSAPP_PHONE_NUMBER_ID") else None

    return _cached(("phone", phone_number_id), load)


def whatsapp_credentials(store: Optional[int] = None) -> dict[str, Optional[str]]:
    """What whatsapp.py needs to send as this store.

    A store's own (encrypted) credentials first. Only the demo store falls back
    to backend/.env: another brand must never send with the demo's token.
    """
    import secrets_box

    sid = store_id() if store is None else store
    row = get_store(sid) or {}
    token = secrets_box.decrypt(row.get("wa_access_token_enc")) if row.get("wa_access_token_enc") else None
    if token and row.get("wa_phone_number_id"):
        return {"phone_number_id": row["wa_phone_number_id"], "access_token": token,
                "display_number": row.get("wa_display_number")}
    if sid == DEMO_STORE_ID:
        return {"phone_number_id": os.environ["WHATSAPP_PHONE_NUMBER_ID"],
                "access_token": os.environ["WHATSAPP_ACCESS_TOKEN"],
                "display_number": os.getenv("WHATSAPP_DISPLAY_NUMBER")}
    raise RuntimeError(f"store {sid} has no WhatsApp credentials (scripts/stores.py set-whatsapp)")


def verify_tokens() -> set[str]:
    """Every webhook verify token Meta may present: the demo's and each store's."""
    import secrets_box
    from db import session_scope
    from models import Store

    tokens = {os.getenv("WHATSAPP_VERIFY_TOKEN", "")}
    with session_scope() as db:
        encrypted = db.exec(select(Store.wa_verify_token_enc).where(Store.wa_verify_token_enc.is_not(None))).all()
    for value in encrypted:
        try:
            tokens.add(secrets_box.decrypt(value) or "")
        except Exception as exc:
            print(f"[tenancy] a store's verify token does not decrypt: {exc!r}")
    tokens.discard("")
    return tokens


def app_secret(store: Optional[int] = None) -> Optional[str]:
    """The Meta app secret that signs webhook POSTs for this store, if configured.

    The demo store's is WHATSAPP_APP_SECRET in backend/.env; a brand's is stored
    encrypted on its row (scripts/stores.py set-whatsapp).
    """
    import secrets_box

    sid = store_id() if store is None else store
    row = get_store(sid) or {}
    if row.get("wa_app_secret_enc"):
        return secrets_box.decrypt(row["wa_app_secret_enc"])
    if sid == DEMO_STORE_ID:
        return os.getenv("WHATSAPP_APP_SECRET") or None
    return None
