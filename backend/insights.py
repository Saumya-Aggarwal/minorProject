"""/admin/insights: what the bot did for the store, counted from turn_traces.

Every message the webhook answers leaves one trace (tracing.py): which route
answered it, each model call with its time, tokens and cost, every check that
stepped in, each catalogue search and what came back. This module turns a
period of them into the owner's numbers. Read only; the store is the current
one (tenancy), and the eval harness's own traces are left out.

The start of P4's brand dashboard (plan.md).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import select

import tenancy
from db import session_scope
from models import TurnTrace, User

USD_INR = 88   # as evals/configs.yaml; approximate

# How a route is described to the owner, and whether code or the model answered
ROUTES = {
    "assistant": ("AI assistant", "ai"),
    "assistant:checkout": ("AI assistant → checkout", "ai"),
    "search": ("Plain search (AI unavailable)", "fallback"),
    "selection": ("Picked an item by number or name", "code"),
    "size": ("Chose a size", "code"),
    "size_guide": ("Size guide", "code"),
    "code": ("Product code from the website", "code"),
    "greeting": ("Greeting", "code"),
    "lookup": ("Cart or orders, answered by code", "code"),
    "lookup:address": ("Delivery address", "code"),
    "lookup:fallback": ("Cart or orders (AI unavailable)", "fallback"),
    "address": ("Delivery address", "code"),
    "link": ("Linked a website account", "code"),
    "buy": ("BUY command", "code"),
    "reorder": ("Buy it again", "code"),
    "reorder:add": ("Buy it again: added", "code"),
    "reorder:more": ("Buy it again: more", "code"),
    "unsupported": ("Photo or voice note (not read yet)", "code"),
    "error": ("Error", "fallback"),
}

# Each check, in the words of what it caught
GUARDS = {
    "amount_retry": "Wrote a price we could not verify → sent back",
    "amount_cut": "Unverified price removed from the reply",
    "claim_retry": "Said it changed the cart when it had not → sent back",
    "claim_cut": "False cart claim removed",
    "size_retry": "Offered a size the item does not come in → sent back",
    "size_cut": "Invented size removed",
    "pay_retry": "Promised a payment button it cannot send → sent back",
    "pay_cut": "Payment promise removed",
    "off_topic": "Off-topic answer (code, essays) replaced",
    "listing_cut": "Typed its own product list → replaced by the real one",
    "products_attached": "Named products → real photos and prices attached",
    "unknown_products": "Named a product we do not sell → removed",
    "nudge_send_reply": "Asked to attach the products properly",
    "repaired_tool_call": "Malformed tool call repaired",
    "tool_use_retry": "Malformed tool call retried",
    "model_dropped": "Model failed → next model tried",
    "rate_wait": "Waited for the free-tier rate limit",
    "fallback": "AI unavailable → plain search answered",
    "empty_message": "Empty reply caught",
}
# Of those, the ones that keep the service up rather than catch a wrong answer
RELIABILITY = {"model_dropped", "rate_wait", "fallback", "repaired_tool_call", "tool_use_retry", "empty_message"}

# What retrieval loosened (SearchResult.relaxed), in the owner's words
_RELAXED = {"attributes": "colour, fabric or fit", "budget": "budget", "category": "garment type",
            "gender": "men's / women's"}


def _route(path: str) -> tuple[str, str]:
    if path in ROUTES:
        return ROUTES[path]
    if path.startswith("command:"):
        return f"{path.split(':', 1)[1].upper()} command", "code"
    if path.startswith("button:"):
        return "Button tap", "code"
    return path or "unknown", "code"


def _percentile(values: list[float], share: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def _masked(number: Optional[str]) -> str:
    """+91 98••••3121: enough to tell customers apart on a projector."""
    if not number:
        return "website"
    return f"+{number[:2]} {number[2:4]}••••{number[-4:]}" if len(number) >= 10 else number


def _asked_for(search: dict[str, Any]) -> str:
    """What a search wanted, in short words: 'Women · Lehenga · red · under ₹3,000'."""
    f = search.get("filters") or {}
    parts = []
    if f.get("gender"):
        parts.append(str(f["gender"]))
    for key in ("categories", "colours", "fabrics", "fits"):
        if f.get(key):
            parts.append(", ".join(map(str, f[key][:2])) if isinstance(f[key], list) else str(f[key]))
    if f.get("occasion"):
        parts.append(str(f["occasion"]))
    if f.get("max_price"):
        parts.append(f"under ₹{float(f['max_price']):,.0f}")
    return " · ".join(parts) or re.sub(r"\s+", " ", str(search.get("query") or "")).strip()[:60]


def summary(days: int = 7, store_id: Optional[int] = None) -> dict[str, Any]:
    store = store_id or tenancy.store_id()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as db:
        rows = db.exec(
            select(TurnTrace, User.whatsapp_number)
            .join(User, User.user_id == TurnTrace.user_id, isouter=True)
            .where(TurnTrace.store_id == store, TurnTrace.created_at >= since, TurnTrace.channel != "eval")
            .order_by(TurnTrace.created_at.desc())
        ).all()
        traces = [(t.model_dump(), number) for t, number in rows]

    turns = [t for t, _ in traces]
    customers = {t["user_id"] for t in turns if t["user_id"]}
    by_kind: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    for t in turns:
        label, kind = _route(t["path"])
        routes[label] += 1
        by_kind[kind] += 1

    latencies = [t["latency_ms"] / 1000 for t in turns if t["latency_ms"]]
    ai_latencies = [t["latency_ms"] / 1000 for t in turns if t["llm_calls"] and t["latency_ms"]]
    code_latencies = [t["latency_ms"] / 1000 for t in turns if not t["llm_calls"] and t["latency_ms"]]
    cost = sum(float(t["cost_usd"] or 0) for t in turns)
    conversations = len(customers) or 1

    guards: Counter[str] = Counter()
    reliability: Counter[str] = Counter()
    for t in turns:
        for g in t["guards"] or []:
            if g.get("name") in RELIABILITY:
                reliability[GUARDS[g["name"]]] += 1
            elif g.get("name") in GUARDS:
                guards[GUARDS[g["name"]]] += 1

    models: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "ms": [], "tokens": 0, "errors": 0})
    for t in turns:
        for call in t["llm_calls"] or []:
            m = models[call.get("model") or "?"]
            m["calls"] += 1
            m["ms"].append(call.get("ms") or 0)
            m["tokens"] += (call.get("tokens_in") or 0) + (call.get("tokens_out") or 0)
            m["errors"] += 1 if call.get("error") else 0
    model_rows = sorted(({"model": name, "calls": m["calls"], "errors": m["errors"], "tokens": m["tokens"],
                          "median_s": round(statistics.median(m["ms"]) / 1000, 1) if m["ms"] else None}
                         for name, m in models.items()), key=lambda r: -r["calls"])

    # Demand we could not meet: a search that came back empty or had to loosen what was asked
    missed: Counter[str] = Counter()
    relaxed_what: dict[str, set[str]] = defaultdict(set)
    searched: Counter[str] = Counter()
    for t in turns:
        for s in t["searches"] or []:
            wanted = _asked_for(s)
            searched[wanted] += 1
            if not s.get("results") or s.get("relaxed"):
                missed[wanted] += 1
                relaxed_what[wanted].update([_RELAXED.get(r, r) for r in s.get("relaxed") or []]
                                            or ["nothing found"])

    recent = []
    for t, number in traces[:25]:
        label, kind = _route(t["path"])
        recent.append({
            "when": t["created_at"].astimezone(timezone(timedelta(hours=5, minutes=30))),
            "customer": _masked(number),
            "question": t["question"] or f"({t['msg_type']})",
            "route": label, "kind": kind,
            "seconds": round(t["latency_ms"] / 1000, 1) if t["latency_ms"] else None,
            "cost_inr": round(float(t["cost_usd"]) * USD_INR, 3) if t["cost_usd"] else None,
            "guards": [GUARDS[g["name"]] for g in t["guards"] or [] if g.get("name") in GUARDS],
            "model": t["model"],
        })

    total = len(turns) or 1
    return {
        "days": days,
        "messages": len(turns),
        "customers": len(customers),
        "by_kind": {k: round(100 * by_kind[k] / total) for k in ("code", "ai", "fallback")},
        "p50_s": round(_percentile(latencies, 0.5), 1) if latencies else None,
        "p95_s": round(_percentile(latencies, 0.95), 1) if latencies else None,
        "ai_p50_s": round(_percentile(ai_latencies, 0.5), 1) if ai_latencies else None,
        "code_p50_s": round(_percentile(code_latencies, 0.5), 2) if code_latencies else None,
        "cost_inr": round(cost * USD_INR, 2),
        "inr_per_100": round(cost * USD_INR * 100 / conversations, 2),
        "wa_messages": sum(t["wa_messages"] or 0 for t in turns),
        "payments_offered": sum(1 for t in turns if t["cta"]),
        "routes": routes.most_common(),
        "routes_max": max(routes.values()) if routes else 1,
        "guards": guards.most_common(),
        "guards_total": sum(guards.values()),
        "reliability": reliability.most_common(),
        "ai_turns": by_kind["ai"],
        "models": model_rows,
        "missed": [{"wanted": w, "times": n, "how": ", ".join(sorted(relaxed_what[w]))}
                   for w, n in missed.most_common(8)],
        "searched": searched.most_common(8),
        "recent": recent,
    }
