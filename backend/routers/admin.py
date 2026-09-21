"""/admin/training: the store owner teaches the bot by rating its replies.

Owned by Dev A. Only signed-in users whose email is in ADMIN_EMAILS
(backend/.env, comma-separated) may open it. What a rating changes is described
in training.py.
"""

import asyncio
import os
from datetime import timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import catalog
import training
from auth import current_user
from templating import templates

router = APIRouter(tags=["admin"])

IST = timezone(timedelta(hours=5, minutes=30))   # times are stored in UTC


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def _guard(request: Request) -> tuple[Optional[dict[str, Any]], Optional[Any]]:
    """(user, None) for an admin, or (None, the response to send instead)."""
    user = current_user(request)
    if user is None:
        return None, RedirectResponse(f"/login?{urlencode({'next': '/admin/training'})}", status_code=303)
    if (user.get("email") or "").lower() not in _admin_emails():
        page = templates.TemplateResponse(request, "admin_training.html",
                                          {"user": user, "forbidden": True}, status_code=403)
        return None, page
    return user, None


@router.get("/admin/training", response_class=HTMLResponse)
async def training_page(request: Request, tab: str = "replies"):
    user, refusal = _guard(request)
    if refusal is not None:
        return refusal
    if tab == "learned":
        rules = await asyncio.to_thread(training.learned_rules)
        for rule in rules:
            rule["created_at"] = rule["created_at"].astimezone(IST)
            rule["product"] = catalog.get_by_id(rule["product_id"]) if rule["product_id"] else None
        return templates.TemplateResponse(request, "admin_training.html",
                                          {"user": user, "tab": "learned", "rules": rules})
    replies = await asyncio.to_thread(training.recent_replies, 40)
    for reply in replies:
        reply["created_at"] = reply["created_at"].astimezone(IST)
        reply["products"] = [p for p in (catalog.get_by_id(pid) for pid in reply["product_ids"]) if p]
        # The product links are long and the photos are shown beside the text
        reply["answer_text"] = "\n".join(
            line for line in (reply["answer"] or "").splitlines() if not line.strip().startswith("http")).strip()
        # A short follow-up is rated together with what it followed, so the
        # rating matches questions like "haldi outfit for my brother around 7k"
        short = len(reply["question"].split()) <= 5
        reply["rated_question"] = (f"{reply['earlier']} {reply['question']}"
                                   if reply["earlier"] and short else reply["question"])
        reply["show_earlier"] = bool(reply["earlier"]) and short
    return templates.TemplateResponse(request, "admin_training.html",
                                      {"user": user, "tab": "replies", "replies": replies})


@router.post("/admin/training/rate")
async def rate(request: Request, reply_id: int = Form(...), question: str = Form(""),
               rating: int = Form(...), product_id: str = Form(""), note: str = Form("")):
    user, refusal = _guard(request)
    if refusal is not None:
        return refusal
    if rating in (1, -1) and (not product_id or catalog.get_by_id(product_id)):
        await asyncio.to_thread(training.rate, "owner", rating, question, product_id, reply_id,
                                user["user_id"], note.strip())
    return RedirectResponse(f"/admin/training#reply-{reply_id}", status_code=303)


@router.post("/admin/training/undo/{feedback_id}")
async def undo(request: Request, feedback_id: int, back: str = Form("replies")):
    user, refusal = _guard(request)
    if refusal is not None:
        return refusal
    await asyncio.to_thread(training.undo, feedback_id)
    return RedirectResponse("/admin/training?tab=learned" if back == "learned" else "/admin/training",
                            status_code=303)
