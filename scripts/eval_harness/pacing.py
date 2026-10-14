"""Keep an eval run inside a provider's rate limits without the bot noticing.

On Groq's free tier each model gets 8,000 tokens a minute and 200,000 a day.
Left alone, the assistant treats a 429 as an outage: it falls back to another
model, or to plain search. In production that is right. In an eval it would
grade the rate limit instead of the bot, and quietly swap the model under test.

So during a run:
  * a token ledger per model waits BEFORE a call that would go over the
    per-minute budget (config `tpm`; none on a paid tier)
  * a 429 for a per-minute limit waits as long as the provider asks and
    retries the SAME model; the bot never sees it
  * a 429 for a per-day limit raises QuotaExhausted. It is a BaseException,
    so it passes through the bot's `except Exception` fallbacks: the case is
    thrown away (never graded) and the run stops, to be resumed later
  * assistant.time is replaced by a clock that leaves out those waits, so
    they never count toward its 25-second turn budget, nor toward latency
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

WINDOW_S = 60.0
SAFETY = 0.9                 # aim below the limit: the provider's count and ours differ a little
EXPECTED_OUT = 400           # completion tokens a call usually adds (reasoning included)
MAX_TPM_RETRIES = 8


class QuotaExhausted(BaseException):
    """The provider's daily allowance for a model is spent. Stop; resume later."""


class PausableClock:
    """Stands in for the `time` module inside bot.assistant.

    perf_counter() leaves out the time the harness spent waiting for rate
    limits; everything else is the real time module.
    """

    def __init__(self) -> None:
        self.paused = 0.0
        self._lock = threading.Lock()

    def perf_counter(self) -> float:
        return time.perf_counter() - self.paused

    def add_pause(self, seconds: float) -> None:
        with self._lock:
            self.paused += seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?h)?(?:\d+(?:\.\d+)?m(?!s))?(?:\d+(?:\.\d+)?s)?|\d+(?:\.\d+)?ms)",
                        re.IGNORECASE)
_DAILY = re.compile(r"per day|\(TPD\)|\(RPD\)|daily", re.IGNORECASE)


def retry_after(text: str) -> Optional[float]:
    """Seconds from "Please try again in 1m2.5s" / "7.5s" / "450ms"."""
    match = _TRY_AGAIN.search(text or "")
    if not match or not match.group(1):
        return None
    value = match.group(1).lower()
    if value.endswith("ms") and not re.search(r"\d[hm]\d", value):
        return float(value[:-2]) / 1000
    seconds = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", value):
        seconds += float(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return seconds


def classify(exc: BaseException) -> str:
    """"daily" | "minute" | "other" for an exception from a model call."""
    text = str(exc)
    status = getattr(exc, "status_code", None)
    if status != 429 and "429" not in text and "rate_limit" not in text.lower():
        return "other"
    return "daily" if _DAILY.search(text) else "minute"


class Pacer:
    """Wraps llm.complete for one run. Thread-safe (the assistant runs in a thread)."""

    def __init__(self, clock: PausableClock, tpm: Optional[int] = None) -> None:
        self.clock = clock
        self.tpm = tpm
        self._used: dict[str, deque[tuple[float, int]]] = {}
        self._lock = threading.Lock()
        self.waited_s = 0.0            # total, for the report
        self.rate_limited = 0          # 429s absorbed
        self.calls = 0

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        time.sleep(seconds)
        self.clock.add_pause(seconds)
        with self._lock:
            self.waited_s += seconds

    def _in_window(self, model: str, now: float) -> int:
        window = self._used.setdefault(model, deque())
        while window and now - window[0][0] > WINDOW_S:
            window.popleft()
        return sum(tokens for _, tokens in window)

    def before(self, model: str, estimate: int) -> None:
        if not self.tpm:
            return
        budget = int(self.tpm * SAFETY)
        while True:
            with self._lock:
                now = time.monotonic()
                used = self._in_window(model, now)
                if used + estimate <= budget or not self._used[model]:
                    return
                oldest = self._used[model][0][0]
            self._wait(max(WINDOW_S - (now - oldest), 0) + 0.2)

    def after(self, model: str, tokens: int) -> None:
        with self._lock:
            self._used.setdefault(model, deque()).append((time.monotonic(), tokens))
            self.calls += 1

    def wrap(self, real: Callable[..., Any], default_model: Callable[[str], str]) -> Callable[..., Any]:
        def complete(role: str, messages: list[dict[str, Any]], model: Optional[str] = None,
                     timeout: float = 12.0, **options: Any) -> Any:
            chosen = model or default_model(role)
            estimate = len(json.dumps(messages, default=str, ensure_ascii=False)) // 3 + EXPECTED_OUT
            for _ in range(MAX_TPM_RETRIES):
                self.before(chosen, estimate)
                try:
                    response = real(role, messages, model=model, timeout=timeout, **options)
                except Exception as exc:
                    kind = classify(exc)
                    if kind == "daily":
                        # The part that says which limit: Groq puts the org id first
                        reason = re.sub(r"\s*in organization `[^`]*`", "", str(exc))
                        reason = re.sub(r"^Error code: 429 - \{'error': \{'message': '", "", reason)
                        raise QuotaExhausted(f"{chosen}: {reason[:300]}") from exc
                    if kind != "minute":
                        raise
                    with self._lock:
                        self.rate_limited += 1
                        # The provider counted this window full: charge it so we wait it out
                        self._used.setdefault(chosen, deque()).append((time.monotonic(), estimate))
                    self._wait((retry_after(str(exc)) or 5.0) + 0.5)
                    continue
                usage = getattr(response, "usage", None)
                self.after(chosen, int(getattr(usage, "total_tokens", 0) or estimate))
                return response
            raise QuotaExhausted(f"{chosen}: still rate-limited after {MAX_TPM_RETRIES} waits")

        return complete
