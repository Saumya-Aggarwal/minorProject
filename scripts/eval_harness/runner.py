"""Play cases through the real bot and grade every turn.

Each turn goes through webhook._receive_change with a Meta-shaped payload, the
same routing a real WhatsApp message takes (commands, lookups, the assistant,
the plain search, buttons, media), answered inline as X-Local-Test would be,
under tracing.channel = "eval". Nothing leaves the machine except the model
calls: payments and WhatsApp sends are fakes.

Three ways a turn can be answered:
  * graded turns without `model`: the model under test, paced (pacing.py)
  * any turn with `model`: a scripted model (the steps given), no quota used
  * prelude turns without `model`: no model at all (commands, plain search),
    so a prelude costs nothing and its state has the shape the code writes

A case gets a fresh customer (state.py), removed afterwards.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

import httpx
from openai.types.chat import ChatCompletion

import catalog
import checkout
import llm
import payments
import tenancy
import tracing
from bot import assistant
from routers import webhook

from . import cases as case_files
from . import checks, state
from .pacing import PausableClock, Pacer, QuotaExhausted


@dataclass
class ModelConfig:
    """One entry of evals/configs.yaml: the model under test and how to reach it."""

    name: str
    model: str
    reasoning_effort: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    tpm: Optional[int] = None                  # tokens per minute to stay under (free tiers)
    price_usd: Optional[list[float]] = None    # per million tokens: input, cached input, output
    notes: str = ""

    def apply(self, environ: dict[str, str]) -> None:
        """Point the chat role at this model only: no fallbacks, so a failure
        is graded as this model's, never quietly answered by another."""
        environ["LLM_CHAT_MODEL"] = self.model
        environ["LLM_CHAT_FALLBACK_MODEL"] = ""
        environ["LLM_FALLBACK_MODEL"] = ""
        environ["LLM_CHAT_REASONING_EFFORT"] = self.reasoning_effort or ""
        environ["LLM_REASONING_EFFORT"] = self.reasoning_effort or ""
        if self.base_url or self.api_key_env:
            import os
            environ["LLM_CHAT_BASE_URL"] = self.base_url or ""
            environ["LLM_CHAT_API_KEY"] = os.environ.get(self.api_key_env or "", "")
        if self.price_usd:
            llm.PRICES[self.model] = tuple(self.price_usd)  # type: ignore[assignment]


# --- a scripted model ---------------------------------------------------------------

def _completion(step: Any) -> ChatCompletion:
    calls = step if isinstance(step, list) else [step]
    content, tool_calls = None, []
    for n, call in enumerate(calls):
        (name, args), = call.items()
        if name == "text":
            content = str(args)
        else:
            tool_calls.append({"id": f"call_{n}", "type": "function",
                               "function": {"name": name, "arguments": json.dumps(args or {})}})
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return ChatCompletion.model_validate({
        "id": "chatcmpl-scripted", "object": "chat.completion", "created": 0, "model": "scripted",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


class ScriptedModel:
    """Stands in for OpenAI(...): hands out the case's steps in order."""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)
        self.overrun = False

    def _next(self, **_: Any) -> ChatCompletion:
        if not self.steps:
            self.overrun = True
            return _completion({"text": "(the scripted model ran out of steps)"})
        return _completion(self.steps.pop(0))

    @property
    def chat(self) -> Any:
        model = self

        class Raw:
            def create(self, **kwargs: Any) -> Any:
                parsed = model._next(**kwargs)
                return SimpleNamespace(parse=lambda: parsed, headers=httpx.Headers({}))

        class Completions:
            with_raw_response = Raw()

            def create(self, **kwargs: Any) -> Any:
                return model._next(**kwargs)

        return SimpleNamespace(completions=Completions())


# --- fakes for everything outward ------------------------------------------------------

async def _no_send(*_: Any, **__: Any) -> dict[str, Any]:
    return {}


async def _fake_checkout_order(order_id: int, amount_inr: float, description: str, notes: Any = None) -> dict:
    return {"id": f"order_eval_{order_id}"}


async def _no_payments(*_: Any, **__: Any) -> Any:
    raise payments.PaymentError("not during an eval")


def _isolate() -> None:
    payments.create_checkout_order = _fake_checkout_order
    payments.fetch_order_payments = _no_payments
    for name in ("send_whatsapp_message", "send_cta_url", "send_image", "send_product_card", "mark_read_and_typing"):
        setattr(webhook, name, _no_send)
    checkout.send_whatsapp_message = _no_send


# --- the runner -------------------------------------------------------------------------

@dataclass
class RunState:
    calls_real: int = 0
    waited_s: float = 0.0
    rate_limited: int = 0


@dataclass
class Runner:
    config: ModelConfig
    store_id: int
    judge: Optional[Callable[[str, str, str], tuple[bool, str]]] = None
    keep: bool = False
    log: Callable[[str], None] = print
    _traces: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        _isolate()
        tracing.channel = "eval"
        tracing.listeners.append(self._traces.append)
        self.clock = PausableClock()
        real_pause = self.clock.add_pause

        def pause(seconds: float) -> None:
            real_pause(seconds)
            tracing.pause(seconds)      # the turn's trace leaves the wait out too

        self.clock.add_pause = pause    # type: ignore[method-assign]
        assistant.time = self.clock     # its 25 s turn budget leaves out rate-limit waits
        self.real_complete = llm.complete
        self.real_client = llm.client
        self.real_enabled = assistant.enabled
        self.pacer = Pacer(self.clock, self.config.tpm)
        self.paced = self.pacer.wrap(self.real_complete, lambda role: llm.config(role).model)
        with tenancy.use_store(self.store_id):
            self.products = {p["id"]: p for p in catalog.get_all()}

    # how a turn is answered ------------------------------------------------------------
    def _mode(self, kind: str, steps: Optional[list[Any]] = None) -> Optional[ScriptedModel]:
        if kind == "real":
            llm.complete, llm.client, assistant.enabled = self.paced, self.real_client, self.real_enabled
            return None
        if kind == "none":
            llm.complete, llm.client = self.real_complete, self.real_client
            assistant.enabled = lambda: False
            return None
        scripted = ScriptedModel(steps or [])
        llm.complete, llm.client = self.real_complete, (lambda role="chat", timeout=12.0: scripted)
        assistant.enabled = lambda: True
        return scripted

    def restore(self) -> None:
        llm.complete, llm.client, assistant.enabled = self.real_complete, self.real_client, self.real_enabled

    def _send(self, phone: str, name: str, turn: dict[str, Any], text: str) -> Optional[Any]:
        """One customer message through the webhook's routing; returns its trace."""
        if "media" in turn:
            message = {"type": turn["media"], turn["media"]: {"id": "eval-media", "mime_type": "image/jpeg"}}
        elif "button" in turn:
            message = {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {
                "id": turn["button"], "title": "Choose" if turn["button"].startswith("pick:") else "Not for me"}}}
        else:
            message = {"type": "text", "text": {"body": text}}
        message.update({"from": phone, "id": f"wamid.eval.{time.time_ns()}", "timestamp": str(int(time.time()))})
        value = {"messaging_product": "whatsapp", "contacts": [{"wa_id": phone, "profile": {"name": name}}],
                 "messages": [message]}
        self._traces.clear()
        with tenancy.use_store(self.store_id):
            asyncio.run(webhook._receive_change(value, self.store_id, True, None, []))
        return self._traces[-1] if self._traces else None

    # a case -----------------------------------------------------------------------------
    def run(self, case: case_files.Case, repeat: int = 1) -> dict[str, Any]:
        result: dict[str, Any] = {"id": case.id if repeat == 1 else f"{case.id}#{repeat}", "case": case.id,
                                  "file": case.file, "tags": case.tags, "source": case.source,
                                  "config": self.config.name, "status": "graded", "turns": [], "warnings": []}
        phone = state.phone_for(self.config.name, case.id, repeat)
        name = case.given.get("name") or "Eval"
        seeded: dict[str, Any] = {}
        said: list[str] = []
        try:
            seeded = state.create_customer(self.store_id, phone, case.given)
            for n, turn in enumerate(case.prelude, 1):
                scripted = self._mode("scripted" if "model" in turn else "none", turn.get("model"))
                text = case_files.fill(turn.get("say") or "", seeded["orders"],
                                       state.snapshot(self.store_id, seeded["user_id"])["session"]["shown"])
                said.append(text)
                trace = self._send(phone, name, turn, text)
                problem = self._script_problem(scripted, trace)
                if problem:
                    result["warnings"].append(f"prelude[{n}] {text!r}: {problem}")
            for turn in case.turns:
                result["turns"].append(self._graded(turn, phone, name, seeded, said))
        except QuotaExhausted as exc:
            result["status"], result["error"] = "quota", str(exc)
            raise
        except case_files.CaseError as exc:
            result["status"], result["error"] = "setup_error", str(exc)
        except Exception as exc:
            result["status"], result["error"] = "setup_error", f"{type(exc).__name__}: {exc}"
            self.log(f"  ! {case.id}: {result['error']}")
        finally:
            self.restore()
            if seeded and not self.keep:
                state.delete_customer(seeded["user_id"])
        if result["status"] == "graded" and result["warnings"]:
            # A prelude that did not play as written leaves the case meaningless
            result["status"] = "setup_error"
            result["error"] = result["warnings"][0]
        result["passed"] = result["status"] == "graded" and all(
            c["ok"] for t in result["turns"] for c in t["checks"])
        return result

    def _script_problem(self, scripted: Optional[ScriptedModel], trace: Any) -> str:
        if trace is None:
            return "no trace (nothing answered)"
        if scripted is None:
            return ""
        if scripted.overrun:
            return "the bot asked the scripted model for more steps than the case gives"
        if scripted.steps:
            return f"{len(scripted.steps)} scripted step(s) unused (path {trace.path!r})"
        return ""

    def _graded(self, turn: dict[str, Any], phone: str, name: str, seeded: dict[str, Any],
                said: list[str]) -> dict[str, Any]:
        before = state.snapshot(self.store_id, seeded["user_id"])
        turn = case_files.fill(turn, seeded["orders"], before["session"]["shown"])
        text = turn.get("say") or ""
        said.append(text)
        scripted = self._mode("scripted" if "model" in turn else "real", turn.get("model"))
        trace = self._send(phone, name, turn, text)
        self.restore()
        after = state.snapshot(self.store_id, seeded["user_id"])
        if trace is None:
            raise case_files.CaseError(f"turn {text!r}: no trace was recorded")
        problem = self._script_problem(scripted, trace) if scripted else ""
        if problem:
            raise case_files.CaseError(f"turn {text!r}: {problem}")
        record = checks.Turn(say=text, reply=trace.reply or {}, trace=_trace_dict(trace), before=before,
                             after=after, products=self.products, customer_said=" ".join(said))
        graded = checks.grade(record, turn.get("expect") or {}, turn.get("skip") or [])
        judged = checks.judged(record, turn.get("judge") or "", self.judge)
        return {
            "say": text if "say" in turn else (turn.get("button") or f"[{turn.get('media')}]"),
            "scripted": scripted is not None,
            "reply": (trace.reply or {}).get("text", ""),
            "shown": (trace.reply or {}).get("product_ids", []),
            "cta": bool((trace.reply or {}).get("cta_url")),
            "trace": _trace_dict(trace),
            "checks": [c.__dict__ for c in graded],
            "judge": judged.__dict__ if judged else None,
        }


def _trace_dict(trace: Any) -> dict[str, Any]:
    return {
        "path": trace.path, "tools": trace.tools, "guards": trace.guards, "searches": trace.searches,
        "llm_calls": [{k: v for k, v in c.items() if k != "limits"} for c in trace.llm_calls],
        "model": trace.model, "tokens_in": trace.tokens_in, "tokens_cached": trace.tokens_cached,
        "tokens_out": trace.tokens_out, "cost_usd": trace.cost_usd, "latency_ms": trace.latency_ms,
        "wa_messages": (trace.reply or {}).get("wa_messages", 0), "error": trace.error,
        "model_message": trace.model_message,
    }
