"""One place that knows which model does which job (plan.md, Architecture).

Each role has its own provider (base URL), key and models, so trying another
model for one job (Kimi for chat, a vision model for photos) is a change in
backend/.env, not in code:

  chat    the assistant with tools, and the one-line intro above a list
  vision  reading a customer's photo into search filters (P6)
  stt     voice notes to text (P6; Sarvam is not OpenAI-compatible, so P6
          adds its own client and reads only the settings here)
  judge   grading replies in the eval harness (P2)

Settings, read on every call (dotenv loads in main.py):
  LLM_<ROLE>_API_KEY, LLM_<ROLE>_BASE_URL, LLM_<ROLE>_MODEL,
  LLM_<ROLE>_FALLBACK_MODEL (comma list), LLM_<ROLE>_REASONING_EFFORT
Models and reasoning effort fall back to the chat role's, and the chat role's
to the original names (LLM_MODEL, LLM_FALLBACK_MODEL, LLM_REASONING_EFFORT,
OPENAI_CHAT_MODEL).

A key and its endpoint always come from the SAME level, so a key is only ever
sent where it was configured to go:
  role     LLM_<ROLE>_API_KEY + LLM_<ROLE>_BASE_URL (no URL = OpenAI's endpoint)
  chat     LLM_CHAT_API_KEY + LLM_CHAT_BASE_URL     (no URL = OpenAI's endpoint)
  generic  LLM_API_KEY + LLM_BASE_URL               (no URL = Groq, as since Sept)
  openai   OPENAI_API_KEY, to OpenAI only
The first level with either value set wins; a role with neither uses chat's.
The OpenAI SDK's own fallbacks (OPENAI_API_KEY / OPENAI_BASE_URL from the
environment when given None) are never used: client() passes both explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

ROLES = ("chat", "vision", "stt", "judge")
GROQ_URL = "https://api.groq.com/openai/v1"
OPENAI_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class RoleConfig:
    role: str
    api_key: Optional[str] = field(repr=False)      # never in logs or tracebacks
    base_url: Optional[str]
    models: list[str] = field(default_factory=list)     # primary first, then fallbacks
    reasoning_effort: Optional[str] = None

    @property
    def model(self) -> str:
        return self.models[0] if self.models else ""


def _pair(prefix: str, default_url: str) -> Optional[tuple[str, Optional[str]]]:
    """(endpoint, key) from one level of settings, or None if it sets neither."""
    url, key = os.getenv(prefix + "BASE_URL"), os.getenv(prefix + "API_KEY")
    if not (url or key):
        return None
    return url or default_url, key or None


def _chat_endpoint_and_key() -> tuple[str, Optional[str]]:
    return (_pair("LLM_CHAT_", OPENAI_URL) or _pair("LLM_", GROQ_URL)
            or (OPENAI_URL, os.getenv("OPENAI_API_KEY") or None))


def _chat() -> RoleConfig:
    groq_key = os.getenv("LLM_API_KEY")
    base_url, api_key = _chat_endpoint_and_key()
    if groq_key:
        primary = os.getenv("LLM_CHAT_MODEL") or os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
        default_fallbacks = "openai/gpt-oss-120b,qwen/qwen3.8-27b"
    else:
        primary = os.getenv("LLM_CHAT_MODEL") or os.getenv("LLM_MODEL") or os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
        default_fallbacks = ""
    fallbacks = os.getenv("LLM_CHAT_FALLBACK_MODEL") or os.getenv("LLM_FALLBACK_MODEL", default_fallbacks)
    models = list(dict.fromkeys([primary, *[m.strip() for m in fallbacks.split(",") if m.strip()]]))
    effort = os.getenv("LLM_CHAT_REASONING_EFFORT") or os.getenv("LLM_REASONING_EFFORT") or None
    return RoleConfig("chat", api_key or None, base_url, models, effort)


def config(role: str = "chat") -> RoleConfig:
    if role not in ROLES:
        raise ValueError(f"unknown LLM role {role!r}; one of {ROLES}")
    chat = _chat()
    if role == "chat":
        return chat
    prefix = f"LLM_{role.upper()}_"
    model = os.getenv(prefix + "MODEL")
    fallbacks = os.getenv(prefix + "FALLBACK_MODEL", "")
    models = ([model, *[m.strip() for m in fallbacks.split(",") if m.strip()]] if model else chat.models)
    # The role's own endpoint and key, or chat's pair: never one of each
    base_url, api_key = _pair(prefix, OPENAI_URL) or (chat.base_url, chat.api_key)
    return RoleConfig(role, api_key, base_url, list(dict.fromkeys(models)),
                      os.getenv(prefix + "REASONING_EFFORT") or chat.reasoning_effort)


def available(role: str = "chat") -> bool:
    return bool(config(role).api_key)


def client(role: str = "chat", timeout: float = 12.0) -> Any:
    """An OpenAI-compatible client for the role (no retries: callers fall back instead)."""
    from openai import OpenAI

    settings = config(role)
    if not settings.api_key:
        # Never let the SDK fill in OPENAI_API_KEY for an endpoint it was not meant for
        raise RuntimeError(f"no API key configured for the {role} role's endpoint {settings.base_url}")
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=timeout, max_retries=0)


# USD per million tokens: (input, cached input, output). Groq's model pages,
# checked 3 Oct 2026; Groq lists no separate cached price, so cached input is
# charged as input. A model not listed has no known cost (None in traces):
# add it here, or in evals/configs.yaml for an eval run.
PRICES: dict[str, tuple[float, float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.15, 0.60),
    "qwen/qwen3.8-27b": (0.80, 0.80, 4.00),
}


def cost_usd(model: str, tokens_in: int, tokens_cached: int, tokens_out: int) -> Optional[float]:
    """What one call cost, or None for a model without a known price."""
    price = PRICES.get(model)
    if price is None:
        return None
    fresh = max(tokens_in - tokens_cached, 0)
    return round((fresh * price[0] + tokens_cached * price[1] + tokens_out * price[2]) / 1_000_000, 8)


def complete(role: str, messages: list[dict[str, Any]], model: Optional[str] = None,
             timeout: float = 12.0, **options: Any) -> Any:
    """One chat completion for a role; the role's reasoning effort is applied.

    Each call, and each failure, is recorded on the current message's trace
    (tracing.py) with its tokens and milliseconds.
    """
    import tracing

    settings = config(role)
    if settings.reasoning_effort:
        options.setdefault("reasoning_effort", settings.reasoning_effort)
    chosen = model or settings.model
    started = tracing.now()
    try:
        # The raw form only to read the rate-limit headers; parse() is the same
        # response object a plain create() returns
        raw = client(role, timeout).chat.completions.with_raw_response.create(
            model=chosen, messages=messages, **options)
        response = raw.parse()
    except Exception as exc:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        tracing.llm_call(role, chosen, tracing.elapsed(started) * 1000, error=str(exc), limits=_limits(headers))
        raise
    tracing.llm_call(role, chosen, tracing.elapsed(started) * 1000, getattr(response, "usage", None),
                     limits=_limits(raw.headers))
    return response


def _limits(headers: Any) -> Optional[dict[str, str]]:
    """The provider's rate-limit headers (Groq: remaining tokens this minute,
    requests today), so a trace shows how close a call came to a 429."""
    if not headers:
        return None
    try:
        found = {name: headers.get(name) for name in (
            "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
            "x-ratelimit-remaining-requests", "retry-after")}
    except Exception:
        return None
    return {k: v for k, v in found.items() if v is not None} or None
