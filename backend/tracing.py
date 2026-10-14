"""One record per customer message: how it was answered, and what that cost.

For every inbound WhatsApp message a Trace collects: which route answered it
(a command, a lookup, the assistant, the plain-search fallback...), every LLM
call (model, milliseconds, tokens, the provider's rate-limit headers), every
tool the assistant used (arguments, result, whether it worked), each catalogue
search (what was asked for, how many matched, which filters were relaxed),
and every check that had to step in (a claim the assistant could not back, an
amount it could not verify, an error swallowed so the customer still got a
reply). The webhook saves it to turn_traces.

Who reads it:
  * the P2 eval harness (scripts/eval_conversations.py), which grades turns
    from these traces instead of from stdout
  * the brand dashboard (P4): chats, share answered by the bot, cost, what
    customers asked for that the catalogue did not have

The trace lives in a ContextVar, like tenancy's current store, so it follows
the message through awaits and into asyncio.to_thread (the assistant runs in
a thread): the worker gets a copy of the context holding the SAME Trace
object, so what it records is seen by the webhook. Code with no trace in its
context (the website, scripts, tests that call the assistant directly)
records nothing: every function here is then a no-op.

Recording must never change a reply or stop one: every helper swallows its
own errors, and save() runs after the reply is built. `_faults` makes every
internal here raise, so scripts/test_trace_neutrality.py can prove that a
customer gets exactly the same answers when tracing is broken.

The clock: now()/elapsed() are wall time minus `paused_s`, time the eval harness
spent waiting out a provider's rate limit. In production nothing pauses, so it
is plain wall time; in an eval, a wait for quota never counts toward the
assistant's 25-second turn budget or the measured latency.
"""

from __future__ import annotations

import dataclasses
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Kept short: a trace is for counting and debugging, not a transcript store
_RESULT_CHARS = 300
_ARG_CHARS = 300


@dataclass
class Trace:
    store_id: int
    channel: str                        # whatsapp | eval
    msg_type: str                       # text | button | image | audio | ...
    question: str
    wa_message_id: Optional[str] = None
    user_id: Optional[int] = None
    path: str = ""                      # which exit of the webhook answered
    tools: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    searches: list[dict[str, Any]] = field(default_factory=list)
    guards: list[dict[str, Any]] = field(default_factory=list)
    # The assistant's own words after its checks (the rest of a reply is code)
    model_message: Optional[str] = None
    attach: Optional[str] = None
    reply_id: Optional[int] = None      # bot_replies row, when the answer was logged
    reply: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started: float = field(default_factory=time.perf_counter)
    paused_s: float = 0.0
    latency_ms: int = 0

    # --- totals -----------------------------------------------------------------
    def _answered(self) -> list[dict[str, Any]]:
        return [c for c in self.llm_calls if not c.get("error")]

    @property
    def tokens_in(self) -> int:
        return sum(c.get("tokens_in") or 0 for c in self._answered())

    @property
    def tokens_cached(self) -> int:
        return sum(c.get("tokens_cached") or 0 for c in self._answered())

    @property
    def tokens_out(self) -> int:
        return sum(c.get("tokens_out") or 0 for c in self._answered())

    @property
    def cost_usd(self) -> Optional[float]:
        costs = [c.get("cost_usd") for c in self._answered()]
        if any(c is None for c in costs):
            return None                  # a model without a known price
        return round(sum(costs), 8)

    @property
    def model(self) -> Optional[str]:
        """The model whose answer the customer got: the last chat call that worked."""
        answered = [c["model"] for c in self._answered() if c.get("role") == "chat"]
        # A tool call the provider rejected and the assistant repaired is still that model's answer
        answered += [g["model"] for g in self.guards if g.get("name") == "repaired_tool_call" and g.get("model")]
        return answered[-1] if answered else None


_current: ContextVar[Optional[Trace]] = ContextVar("current_trace", default=None)

# What new traces are tagged with; the eval harness sets "eval"
channel = "whatsapp"
# Called with every finished trace (the eval harness collects them here)
listeners: list[Callable[[Trace], None]] = []
# Tests only: every internal raises (scripts/test_trace_neutrality.py)
_faults = False


def _check() -> None:
    if _faults:
        raise RuntimeError("tracing fault injected by a test")


def current() -> Optional[Trace]:
    return _current.get()


def begin(store_id: int, question: str, msg_type: str = "text",
          wa_message_id: Optional[str] = None, via: Optional[str] = None) -> tuple[Optional[Trace], Any]:
    """Start a trace for one inbound message. Returns it and the reset token,
    or (None, None) if that failed: end() and save() then do nothing.

    `via` names a local channel ("local" for X-Local-Test); it never overrides
    a channel the eval harness has set.
    """
    try:
        _check()
        trace = Trace(store_id=store_id, channel=via if (via and channel == "whatsapp") else channel,
                      msg_type=msg_type, question=question or "", wa_message_id=wa_message_id or None)
        return trace, _current.set(trace)
    except Exception as exc:
        print(f"[tracing] could not start a trace: {exc!r}")
        return None, None


def end(trace: Optional[Trace], token: Any, reply: Any = None, error: str = "") -> None:
    """Close the trace: latency, the reply as sent (a str or webhook.Reply), listeners told."""
    if trace is None:
        return
    try:
        _check()
        trace.latency_ms = round(elapsed(trace.started, trace) * 1000)
        if reply is not None:
            trace.reply = describe(reply)
        if error:
            trace.error = error[:500]
        for listener in list(listeners):
            listener(trace)
    except Exception as exc:
        print(f"[tracing] could not finish a trace: {exc!r}")
    finally:
        try:
            _current.reset(token)
        except Exception:
            # A token from another context; clearing is the safe fallback
            _current.set(None)


def now(trace: Optional[Trace] = None) -> float:
    """The trace's clock: perf_counter minus the harness pauses so far.

    Take a start time from here and pass it to elapsed(): only pauses that
    happen in between are left out. (A per-call time that subtracted every
    pause of the turn, including earlier ones, once came out negative.)
    """
    try:
        _check()
        trace = trace or _current.get()
        paused = trace.paused_s if trace is not None else 0.0
    except Exception:
        paused = 0.0
    return time.perf_counter() - paused


def elapsed(started: float, trace: Optional[Trace] = None) -> float:
    """Seconds since `started`, a value from now() (a Trace's own `started`
    is one: nothing has paused when it begins), minus pauses since then."""
    return now(trace) - started


def pause(seconds: float) -> None:
    """The eval harness waited this long for a rate limit (not the bot's time)."""
    _safe(lambda t: setattr(t, "paused_s", t.paused_s + seconds))


def _safe(record: Callable[[Trace], None]) -> None:
    trace = _current.get()
    if trace is None:
        return
    try:
        _check()
        record(trace)
    except Exception as exc:
        print(f"[tracing] could not record: {exc!r}")


def path(name: str) -> None:
    """Which exit of the webhook answered. The last one set wins."""
    _safe(lambda t: setattr(t, "path", name))


def set_user(user_id: Optional[int]) -> None:
    _safe(lambda t: setattr(t, "user_id", user_id))


def note(**fields: Any) -> None:
    """Set model_message / attach / reply_id on the trace."""
    def record(t: Trace) -> None:
        for key, value in fields.items():
            if key in ("model_message", "attach", "reply_id"):
                setattr(t, key, value)
    _safe(record)


def guard(name: str, **detail: Any) -> None:
    """A check stepped in, or an error was swallowed so the customer still got a reply."""
    _safe(lambda t: t.guards.append({"name": name, **{k: _short(v, _ARG_CHARS) for k, v in detail.items()}}))


def swallowed(where: str, exc: BaseException) -> None:
    """An exception the webhook or assistant caught and answered around."""
    guard("swallowed", where=where, error=repr(exc))


def tool(name: str, args: dict[str, Any], result: str, ms: float) -> None:
    def record(t: Trace) -> None:
        t.tools.append({
            "name": name,
            # Not the customer's words the assistant adds to cart tools: the trace has the question
            "args": {k: _short(v, _ARG_CHARS) for k, v in (args or {}).items()
                     if k not in ("customer_text", "customer_recent", "ledger")},
            "ok": not str(result).startswith("ERROR"),
            "result": _short(result, _RESULT_CHARS),
            "ms": round(ms),
        })
    _safe(record)


def search(query: str, filters: Any, results: list[Any], relaxed: list[str]) -> None:
    """One catalogue search: what was asked for (filters: a dict or retrieval.Filters)
    and what came back (product dicts or ids)."""
    def record(t: Trace) -> None:
        wanted = dataclasses.asdict(filters) if dataclasses.is_dataclass(filters) else dict(filters or {})
        t.searches.append({
            "query": _short(query, _ARG_CHARS),
            "filters": _short({k: v for k, v in wanted.items() if v not in (None, "", [], False)}, _ARG_CHARS),
            "results": [r.get("id") if isinstance(r, dict) else r for r in list(results)[:12]],
            "relaxed": list(relaxed),
        })
    _safe(record)


def llm_call(role: str, model: str, ms: float, usage: Any = None, error: Optional[str] = None,
             limits: Optional[dict[str, str]] = None) -> None:
    """One model call. usage is the SDK's usage object (None when the call failed)."""
    def record(t: Trace) -> None:
        tokens_in = _get(usage, "prompt_tokens")
        tokens_out = _get(usage, "completion_tokens")
        cached = _get(_get(usage, "prompt_tokens_details"), "cached_tokens")
        reasoning = _get(_get(usage, "completion_tokens_details"), "reasoning_tokens")
        entry: dict[str, Any] = {
            "role": role, "model": model, "ms": round(ms),
            "tokens_in": tokens_in, "tokens_cached": cached, "tokens_out": tokens_out,
            "tokens_reasoning": reasoning,
        }
        if limits:
            entry["limits"] = limits
        if error:
            entry["error"] = _short(error, 300)
        else:
            import llm
            entry["cost_usd"] = llm.cost_usd(model, tokens_in or 0, cached or 0, tokens_out or 0)
        t.llm_calls.append(entry)
    _safe(record)


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _short(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_short(v, limit) for v in list(value)[:20]]
    if isinstance(value, dict):
        return {str(k): _short(v, limit) for k, v in list(value.items())[:20]}
    return _short(str(value), limit)


def wa_messages(reply: Any) -> int:
    """How many WhatsApp messages a reply takes (each is billed from 1 Oct 2026)."""
    _check()
    cards = getattr(reply, "cards", None) or []
    if cards:
        return int(bool(getattr(reply, "lead", ""))) + len(cards) + int(bool(getattr(reply, "tail", "")))
    return 1 if (getattr(reply, "text", reply) or "") else 0


def describe(reply: Any) -> dict[str, Any]:
    """The reply as sent, in plain data (a str, or webhook.Reply)."""
    _check()
    if reply is None:
        return {}
    if isinstance(reply, str):
        return {"text": reply, "wa_messages": 1 if reply else 0}
    cards = getattr(reply, "cards", None) or []
    return {
        "text": getattr(reply, "text", ""),
        "lead": getattr(reply, "lead", ""),
        "tail": getattr(reply, "tail", ""),
        "cta_url": getattr(reply, "cta_url", None),
        "cta_label": getattr(reply, "cta_label", None) if getattr(reply, "cta_url", None) else None,
        "product_ids": [c.product_id for c in cards],
        "cards": [{"product_id": c.product_id, "caption": c.caption, "buttons": c.buttons} for c in cards],
        "wa_messages": wa_messages(reply),
    }


def save(trace: Optional[Trace]) -> None:
    """Write the trace to turn_traces. Never raises: a lost trace beats a lost reply."""
    if trace is None:
        return
    try:
        _check()
        from db import session_scope
        from models import TurnTrace

        reply = trace.reply or {}
        with session_scope() as db:
            db.add(TurnTrace(
                store_id=trace.store_id, user_id=trace.user_id, channel=trace.channel,
                msg_type=trace.msg_type, wa_message_id=trace.wa_message_id,
                question=trace.question[:1000], path=(trace.path or "unknown")[:40],
                reply_text=str(reply.get("text") or "")[:3000], reply_id=trace.reply_id,
                product_ids=list(reply.get("product_ids") or []), searches=trace.searches,
                cta=bool(reply.get("cta_url")), wa_messages=int(reply.get("wa_messages") or 0),
                tools=trace.tools, llm_calls=trace.llm_calls, guards=trace.guards,
                model=trace.model, tokens_in=trace.tokens_in, tokens_cached=trace.tokens_cached,
                tokens_out=trace.tokens_out, cost_usd=trace.cost_usd, latency_ms=trace.latency_ms,
                error=trace.error or None,
            ))
    except Exception as exc:
        print(f"[tracing] could not save the trace: {exc!r}")
