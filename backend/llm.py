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
Anything unset falls back to the chat role's — except the key of a role that
sets its own base URL, which must set its own key too — and the chat role to
the original names: LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_FALLBACK_MODEL /
LLM_REASONING_EFFORT, or OPENAI_API_KEY + OPENAI_CHAT_MODEL alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

ROLES = ("chat", "vision", "stt", "judge")
GROQ_URL = "https://api.groq.com/openai/v1"


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


def _chat() -> RoleConfig:
    groq_key = os.getenv("LLM_API_KEY")
    api_key = os.getenv("LLM_CHAT_API_KEY") or groq_key or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("LLM_CHAT_BASE_URL") or os.getenv("LLM_BASE_URL") or (GROQ_URL if groq_key else None)
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
    own_url = os.getenv(prefix + "BASE_URL")
    # The chat key is borrowed only together with the chat endpoint: a role
    # pointed at another provider without its own key gets no key, rather
    # than sending the chat provider's key to a third party
    api_key = os.getenv(prefix + "API_KEY") or (None if own_url else chat.api_key)
    return RoleConfig(role, api_key, own_url or chat.base_url, list(dict.fromkeys(models)),
                      os.getenv(prefix + "REASONING_EFFORT") or chat.reasoning_effort)


def available(role: str = "chat") -> bool:
    return bool(config(role).api_key)


def client(role: str = "chat", timeout: float = 12.0) -> Any:
    """An OpenAI-compatible client for the role (no retries: callers fall back instead)."""
    from openai import OpenAI

    settings = config(role)
    arguments: dict[str, Any] = {"api_key": settings.api_key, "timeout": timeout, "max_retries": 0}
    if settings.base_url:
        arguments["base_url"] = settings.base_url
    return OpenAI(**arguments)


def complete(role: str, messages: list[dict[str, Any]], model: Optional[str] = None,
             timeout: float = 12.0, **options: Any) -> Any:
    """One chat completion for a role; the role's reasoning effort is applied."""
    settings = config(role)
    if settings.reasoning_effort:
        options.setdefault("reasoning_effort", settings.reasoning_effort)
    return client(role, timeout).chat.completions.create(
        model=model or settings.model, messages=messages, **options)
