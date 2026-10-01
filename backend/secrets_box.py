"""Encrypt the secrets stored on a store row (WhatsApp token, app secret...).

Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`): a database dump or
backup alone does not leak a brand's credentials. The key lives only in the
environment:

    MUNIM_SECRET_KEYS=<newest key>,<older key>,...

The first key encrypts; every key listed can decrypt. Rotation:
  1. generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. put it FIRST in MUNIM_SECRET_KEYS, keep the old one after it, restart
  3. run  scripts/stores.py rotate-keys  (re-encrypts every secret with the new key)
  4. remove the old key from MUNIM_SECRET_KEYS, restart
Losing every key means re-entering each brand's credentials; nothing else.
"""

from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretsUnavailable(RuntimeError):
    pass


def _box() -> MultiFernet:
    keys = [k.strip() for k in os.getenv("MUNIM_SECRET_KEYS", "").split(",") if k.strip()]
    if not keys:
        raise SecretsUnavailable("MUNIM_SECRET_KEYS is not set (see backend/secrets_box.py)")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(value: Optional[str]) -> Optional[str]:
    return None if value is None else _box().encrypt(value.encode()).decode()


def decrypt(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    try:
        return _box().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsUnavailable("a stored secret does not decrypt with MUNIM_SECRET_KEYS") from exc


def rotate(token: Optional[str]) -> Optional[str]:
    """Re-encrypt with the first key (works for tokens made by any listed key)."""
    return None if token is None else _box().rotate(token.encode()).decode()
