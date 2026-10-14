"""The LLM judge: only for what code cannot settle (was the advice sensible,
did it ask the right question), and only on turns that carry a `judge` rubric.

It uses the llm.py "judge" role (LLM_JUDGE_MODEL, LLM_JUDGE_BASE_URL,
LLM_JUDGE_API_KEY). It should be a different model family from the one under
test, or it grades its own habits; the runner warns when they are the same.
Before its scores decide anything, calibrate it against the owner's own
labels (evals/raw feedback): a judge that disagrees with the owner is noise.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import llm

PROMPT = """You grade one reply from a WhatsApp shopping assistant for an Indian clothing shop.
Judge ONLY the criterion given. Facts such as prices, sizes and stock are checked elsewhere: ignore them.

Criterion: {rubric}

Customer: {say}
Assistant: {reply}

Answer on the first line with PASS or FAIL, then one short sentence why."""


def make(complete: Callable[..., Any]) -> Callable[[str, str, str], tuple[bool, str]]:
    def judge(rubric: str, say: str, reply: str) -> tuple[bool, str]:
        response = complete("judge", [{"role": "user", "content": PROMPT.format(
            rubric=rubric, say=say, reply=reply or "(no reply)")}], timeout=30.0, temperature=0, max_tokens=300)
        text = (response.choices[0].message.content or "").strip()
        verdict = re.match(r"\W*(PASS|FAIL)\b", text, re.IGNORECASE)
        if not verdict:
            return False, f"unreadable verdict: {text[:120]!r}"
        return verdict.group(1).upper() == "PASS", text[verdict.end():].strip(" :.-\n")[:200]
    return judge


def same_family(chat_model: str) -> bool:
    judge_model = llm.config("judge").model
    family = lambda m: re.split(r"[-/:]", m.lower().split("/")[-1])[0]  # noqa: E731
    return family(judge_model) == family(chat_model)
