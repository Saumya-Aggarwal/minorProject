"""Teaching the bot: the store owner's ratings, and customers' "Not for me".

This does not fine-tune the language model — a few dozen ratings cannot, and
the free Groq tier does not allow it. It changes what the model is SHOWN:

  * ranking   A product the owner marked "wrong for this question" is pushed
              to the bottom for every question similar to that one (embedding
              cosine >= SIMILAR); one marked "good match" moves up. Similar,
              not identical: "saree for office" and "office saree for work"
              share a rating, "sherwani for my wedding" does not.
  * examples  Whole replies the owner liked are given to the assistant as
              examples when a similar question comes in; notes on replies the
              owner disliked ("ask the budget first") are given as guidance.
  * personal  A customer's "Not for me" hides that product from that customer
              only. Customer feedback never changes anyone else's results, so
              one customer cannot skew the shop.

Every function here is best-effort for the bot: if Postgres is unreachable,
search and replies carry on untrained rather than failing.
"""

from __future__ import annotations

import math
import time
from datetime import timedelta
from functools import lru_cache
from typing import Any, Optional

from sqlmodel import select

from common import embeddings
from db import session_scope
from models import BotReply, Feedback, User, utcnow

SIMILAR = 0.75          # cosine similarity at which two questions count as the same
CACHE_SECONDS = 30      # owner rules are re-read at most this often

_rules_cache: dict[str, Any] = {"at": 0.0, "rules": []}


@lru_cache(maxsize=512)
def _embed(text: str) -> tuple[float, ...]:
    return tuple(embeddings.embed_one(text.strip().lower()))


def _cosine(a: Any, b: Any) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


# --- recording -------------------------------------------------------------------

def log_reply(user_id: int, question: str, answer: str, product_ids: list[str], path: str) -> int:
    """Keep what the bot answered, for the owner to review and rate."""
    with session_scope() as db:
        row = BotReply(user_id=user_id, question=question[:1000], answer=answer[:3000],
                       product_ids=list(product_ids), path=path)
        db.add(row)
        db.flush()
        return row.reply_id


def rate(scope: str, rating: int, question: str, product_id: str = "", reply_id: Optional[int] = None,
         user_id: Optional[int] = None, note: str = "") -> int:
    """Store a rating. The owner rating the same thing again replaces the old one."""
    if scope not in ("owner", "customer") or rating not in (1, -1):
        raise ValueError("scope must be owner|customer and rating +1|-1")
    vector = list(_embed(question)) if question.strip() else None
    with session_scope() as db:
        if scope == "owner" and reply_id is not None:
            for old in db.exec(select(Feedback).where(
                    Feedback.scope == "owner", Feedback.reply_id == reply_id,
                    Feedback.product_id == product_id)).all():
                db.delete(old)
        row = Feedback(scope=scope, rating=rating, product_id=product_id, question=question[:1000],
                       embedding=vector, note=note[:500], reply_id=reply_id, user_id=user_id,
                       created_at=utcnow())
        db.add(row)
        db.flush()
        feedback_id = row.feedback_id
    _rules_cache["at"] = 0.0
    return feedback_id


def undo(feedback_id: int) -> None:
    with session_scope() as db:
        row = db.get(Feedback, feedback_id)
        if row is not None:
            db.delete(row)
    _rules_cache["at"] = 0.0


# --- reading, for the bot ----------------------------------------------------------

def _owner_rules() -> list[dict[str, Any]]:
    if time.time() - _rules_cache["at"] < CACHE_SECONDS:
        return _rules_cache["rules"]
    with session_scope() as db:
        rows = db.exec(select(Feedback, BotReply)
                       .join(BotReply, Feedback.reply_id == BotReply.reply_id, isouter=True)
                       .where(Feedback.scope == "owner")).all()
        rules = [{
            "id": f.feedback_id, "rating": f.rating, "product_id": f.product_id,
            "question": f.question, "embedding": f.embedding, "note": f.note,
            "answer": reply.answer if reply else "",
        } for f, reply in rows if f.embedding]
    _rules_cache.update(at=time.time(), rules=rules)
    return rules


def ranking_adjustments(question: str) -> dict[str, int]:
    """product_id -> net owner rating for questions like this one (+ up, - down)."""
    try:
        rules = [r for r in _owner_rules() if r["product_id"]]
        if not rules or not question.strip():
            return {}
        vector = _embed(question)
        net: dict[str, int] = {}
        for rule in rules:
            if _cosine(vector, rule["embedding"]) >= SIMILAR:
                net[rule["product_id"]] = net.get(rule["product_id"], 0) + rule["rating"]
        return {pid: score for pid, score in net.items() if score}
    except Exception as exc:
        print(f"[training] ranking rules unavailable: {exc!r}")
        return {}


def guidance(question: str, k: int = 2) -> dict[str, list[str]]:
    """Owner-approved replies and owner notes for questions like this one.

    {"examples": ["Customer: ...\\nYou: ..."], "notes": ["ask the budget first"]}
    """
    try:
        rules = [r for r in _owner_rules() if not r["product_id"]]
        if not rules or not question.strip():
            return {"examples": [], "notes": []}
        vector = _embed(question)
        scored = sorted(((_cosine(vector, r["embedding"]), r) for r in rules),
                        key=lambda pair: -pair[0])
        close = [r for score, r in scored if score >= SIMILAR - 0.1]
        examples = [f"Customer: {r['question'][:160]}\nYou: {r['answer'].split(chr(10) + chr(10))[0][:220]}"
                    for r in close if r["rating"] > 0 and r["answer"]][:k]
        notes = [r["note"][:160] for r in close if r["note"]][:k]
        return {"examples": examples, "notes": notes}
    except Exception as exc:
        print(f"[training] guidance unavailable: {exc!r}")
        return {"examples": [], "notes": []}


def hidden_for(user_id: Optional[int]) -> set[str]:
    """Products this customer said "Not for me" to."""
    if user_id is None:
        return set()
    try:
        with session_scope() as db:
            rows = db.exec(select(Feedback.product_id).where(
                Feedback.scope == "customer", Feedback.user_id == user_id,
                Feedback.rating < 0, Feedback.product_id != "")).all()
        return set(rows)
    except Exception as exc:
        print(f"[training] hidden products unavailable: {exc!r}")
        return set()


# --- reading, for the admin page -----------------------------------------------------

def recent_replies(limit: int = 40) -> list[dict[str, Any]]:
    """Latest bot replies with the owner's ratings on each, newest first."""
    with session_scope() as db:
        replies = db.exec(select(BotReply, User).join(User, BotReply.user_id == User.user_id)
                          .order_by(BotReply.created_at.desc()).limit(limit)).all()
        ids = [reply.reply_id for reply, _ in replies]
        ratings: dict[int, dict[str, dict[str, Any]]] = {}
        if ids:
            for f in db.exec(select(Feedback).where(Feedback.scope == "owner",
                                                   Feedback.reply_id.in_(ids))).all():
                ratings.setdefault(f.reply_id, {})[f.product_id] = {
                    "id": f.feedback_id, "rating": f.rating, "note": f.note}
        result = []
        for reply, user in replies:
            # A follow-up like "around 7k" means nothing on its own: fetch the
            # customer's message just before it (same chat, within 30 minutes)
            earlier = db.exec(select(BotReply.question).where(
                BotReply.user_id == reply.user_id, BotReply.reply_id < reply.reply_id,
                BotReply.created_at >= reply.created_at - timedelta(minutes=30))
                .order_by(BotReply.reply_id.desc()).limit(1)).first()
            result.append({
                "reply_id": reply.reply_id, "question": reply.question, "answer": reply.answer,
                "earlier": earlier or "",
                "product_ids": reply.product_ids, "path": reply.path, "created_at": reply.created_at,
                "customer": user.display_name or user.email or user.whatsapp_number,
                "ratings": ratings.get(reply.reply_id, {}),
            })
        return result


def learned_rules() -> list[dict[str, Any]]:
    """Every owner rating in force, newest first, for the "What it has learned" tab."""
    with session_scope() as db:
        rows = db.exec(select(Feedback).where(Feedback.scope == "owner")
                       .order_by(Feedback.created_at.desc())).all()
        return [{"id": f.feedback_id, "rating": f.rating, "product_id": f.product_id,
                 "question": f.question, "note": f.note, "created_at": f.created_at} for f in rows]


def owner_rules_for_eval() -> list[dict[str, Any]]:
    """(question, product, rating) triples, so the retrieval eval can hold them."""
    return [r for r in learned_rules() if r["product_id"]]
