"""Website authentication: password hashing and cookie-session helpers.

bcrypt is used directly rather than through passlib, whose 1.7.4 release logs a
spurious version-detection error against bcrypt 4.x.

Note the asymmetry with the bot: the website has sessions and cookies, WhatsApp
has neither. Linking the two is what link_tokens exists for.
"""

import os
from typing import Any, Optional

import bcrypt
from fastapi import Request
from sqlmodel import select

from db import session_scope
from models import User, utcnow

SESSION_USER_KEY = "user_id"

# bcrypt silently truncates at 72 bytes, so reject longer input rather than
# accepting a password whose tail never gets checked
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        # WhatsApp-only account with no password set — cannot log in via the site
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database
        return False


def validate_credentials(email: str, password: str) -> Optional[str]:
    """Return an error message, or None when the input is acceptable."""
    if "@" not in email or len(email) < 5:
        return "Please enter a valid email address."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "Password is too long (72 bytes maximum)."
    return None


def session_secret() -> str:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        # Dev fallback: works, but logs everyone out on restart
        print("[auth] SESSION_SECRET not set — using an ephemeral dev secret")
        secret = os.urandom(32).hex()
    return secret


def login_user(request: Request, user_id: int) -> None:
    request.session[SESSION_USER_KEY] = user_id


def logout_user(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def current_user(request: Request) -> Optional[dict[str, Any]]:
    """The logged-in user as a plain dict, or None.

    Returns a dict rather than a User instance so callers never touch a detached
    SQLAlchemy object after its session has closed.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None

    with session_scope() as db:
        user = db.get(User, user_id)
        if user is None:
            # Row deleted (or merged away) since the cookie was issued
            request.session.pop(SESSION_USER_KEY, None)
            return None

        user.last_active_at = utcnow()
        return {
            "user_id": user.user_id,
            "email": user.email,
            "display_name": user.display_name,
            "whatsapp_number": user.whatsapp_number,
        }


def find_user_by_email(email: str) -> Optional[dict[str, Any]]:
    with session_scope() as db:
        user = db.exec(select(User).where(User.email == email.lower().strip())).first()
        if user is None:
            return None
        return {
            "user_id": user.user_id,
            "email": user.email,
            "display_name": user.display_name,
            "password_hash": user.password_hash,
        }


def create_web_user(email: str, password: str, display_name: str) -> int:
    with session_scope() as db:
        user = User(
            email=email.lower().strip(),
            password_hash=hash_password(password),
            display_name=display_name.strip() or None,
        )
        db.add(user)
        db.flush()
        return user.user_id
