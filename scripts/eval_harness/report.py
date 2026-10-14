"""Turn graded cases into a summary, a Markdown report and a baseline comparison.

A case passes when every check of every turn passes. Model choice (P2 step 5)
ranks configs by: code-check pass rate, then judge score, p95 latency, and
rupees per 100 conversations, all of which are here.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any, Optional


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "-"


def _p(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarise(results: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    graded = [r for r in results if r["status"] == "graded"]
    turns = [t for r in graded for t in r["turns"]]
    # Turns the model under test answered (code answers commands, lookups, buttons without one)
    real_turns = [t for t in turns if not t["scripted"] and t["trace"]["llm_calls"]]
    per_check: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "fails": 0, "universal": 0})
    for t in turns:
        for c in t["checks"]:
            entry = per_check[c["name"]]
            entry["runs"] += 1
            entry["fails"] += 0 if c["ok"] else 1
            entry["universal"] = int(c.get("universal", False))
    per_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"cases": 0, "passed": 0})
    for r in graded:
        for tag in r["tags"] or ["untagged"]:
            per_tag[tag]["cases"] += 1
            per_tag[tag]["passed"] += int(r["passed"])
    judged = [t["judge"] for t in turns if t.get("judge")]
    guards = Counter(g["name"] for t in real_turns for g in t["trace"]["guards"])

    costs = []
    for r in graded:
        case_costs = [t["trace"]["cost_usd"] for t in r["turns"] if not t["scripted"]]
        if case_costs and all(c is not None for c in case_costs):
            costs.append(sum(case_costs))
    latency = [t["trace"]["latency_ms"] for t in real_turns]
    usd_inr = float(meta.get("usd_inr") or 0)

    checks_total = sum(v["runs"] for v in per_check.values())
    checks_failed = sum(v["fails"] for v in per_check.values())
    return {
        **meta,
        "cases": {"total": len(results), "graded": len(graded), "passed": sum(r["passed"] for r in graded),
                  "setup_errors": sum(r["status"] == "setup_error" for r in results),
                  "not_run": sum(r["status"] == "quota" for r in results)},
        "checks": {"total": checks_total, "failed": checks_failed,
                   "pass_rate": round(1 - checks_failed / checks_total, 4) if checks_total else None},
        "per_check": dict(sorted(per_check.items())),
        "per_tag": dict(sorted(per_tag.items())),
        "judge": {"judged": len(judged), "passed": sum(j["ok"] for j in judged)},
        "metrics": {
            "turns": len(turns), "model_turns": len(real_turns),
            "latency_ms_p50": _p(latency, 0.5), "latency_ms_p95": _p(latency, 0.95),
            "llm_calls_per_model_turn": round(statistics.mean([len(t["trace"]["llm_calls"]) for t in real_turns]), 2)
            if real_turns else None,
            "tokens_in_per_model_turn": round(statistics.mean([t["trace"]["tokens_in"] for t in real_turns]))
            if real_turns else None,
            "tokens_out_per_model_turn": round(statistics.mean([t["trace"]["tokens_out"] for t in real_turns]))
            if real_turns else None,
            "usd_per_conversation": round(statistics.mean(costs), 6) if costs else None,
            "inr_per_100_conversations": round(statistics.mean(costs) * 100 * usd_inr, 2)
            if costs and usd_inr else None,
            "wa_messages_per_turn": round(statistics.mean([t["trace"]["wa_messages"] or 0 for t in turns]), 2)
            if turns else None,
            "guards": dict(guards.most_common()),
        },
        "case_results": {r["id"]: r["passed"] for r in graded},
    }


def compare(now: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    before, after = baseline.get("case_results", {}), now.get("case_results", {})
    return {
        "baseline": f"{baseline.get('config')} on {baseline.get('started', '?')}",
        "regressions": sorted(c for c, ok in after.items() if before.get(c) is True and not ok),
        "fixed": sorted(c for c, ok in after.items() if before.get(c) is False and ok),
        "new": sorted(c for c in after if c not in before),
        "pass_rate": (baseline.get("checks", {}).get("pass_rate"), now.get("checks", {}).get("pass_rate")),
    }


def markdown(summary: dict[str, Any], results: list[dict[str, Any]], diff: Optional[dict[str, Any]] = None) -> str:
    c, m = summary["cases"], summary["metrics"]
    lines = [
        f"# Eval: {summary['brand']} · {summary['config']} ({summary.get('model')})",
        "",
        f"Started {summary.get('started')} · {summary.get('cases_dir')} · run `{summary.get('run_dir')}`",
        "",
        f"**Cases passed: {c['passed']}/{c['graded']}** ({_pct(c['passed'], c['graded'])})"
        + (f" · {c['setup_errors']} setup error(s)" if c["setup_errors"] else "")
        + (f" · {c['not_run']} not run (quota)" if c["not_run"] else ""),
        f"**Checks passed: {summary['checks']['total'] - summary['checks']['failed']}/{summary['checks']['total']}**"
        f" ({_pct(summary['checks']['total'] - summary['checks']['failed'], summary['checks']['total'])})",
    ]
    if summary["judge"]["judged"]:
        lines.append(f"**Judge: {summary['judge']['passed']}/{summary['judge']['judged']}**")
    lines += [
        "",
        "| Metric | Value |", "|---|---|",
        f"| Turns graded | {m['turns']} ({m['model_turns']} answered by the model, the rest by code) |",
        f"| Latency p50 / p95 (model turns) | {_ms(m['latency_ms_p50'])} / {_ms(m['latency_ms_p95'])} |",
        f"| LLM calls per model turn | {m['llm_calls_per_model_turn']} |",
        f"| Tokens in / out per model turn | {m['tokens_in_per_model_turn']} / {m['tokens_out_per_model_turn']} |",
        f"| Cost per conversation | {_usd(m['usd_per_conversation'])} |",
        f"| ₹ per 100 conversations | {m['inr_per_100_conversations'] if m['inr_per_100_conversations'] is not None else '-'} |",
        f"| WhatsApp messages per turn | {m['wa_messages_per_turn']} |",
        f"| Rate-limit wait (excluded from latency) | {summary.get('waited_s', 0):.0f} s |",
        "",
    ]
    if diff:
        lines += [f"## Against the baseline ({diff['baseline']})", "",
                  f"- Check pass rate: {diff['pass_rate'][0]} → {diff['pass_rate'][1]}",
                  f"- **Regressions ({len(diff['regressions'])}):** {', '.join(diff['regressions']) or 'none'}",
                  f"- Fixed ({len(diff['fixed'])}): {', '.join(diff['fixed']) or 'none'}",
                  f"- New cases: {', '.join(diff['new']) or 'none'}", ""]
    lines += ["## Checks", "", "| Check | Failed | Runs | Kind |", "|---|---|---|---|"]
    for name, v in sorted(summary["per_check"].items(), key=lambda kv: -kv[1]["fails"]):
        lines.append(f"| {name} | {v['fails']} | {v['runs']} | {'every turn' if v['universal'] else 'case'} |")
    lines += ["", "## By tag", "", "| Tag | Passed | Cases |", "|---|---|---|"]
    for tag, v in summary["per_tag"].items():
        lines.append(f"| {tag} | {v['passed']} | {v['cases']} |")
    if m["guards"]:
        lines += ["", "## Checks inside the bot that stepped in (model turns)", "",
                  ", ".join(f"{k} ×{v}" for k, v in m["guards"].items())]
    lines += ["", "## Cases", ""]
    for r in results:
        mark = "✅" if r.get("passed") else ("⚠️" if r["status"] != "graded" else "❌")
        lines.append(f"### {mark} {r['id']}" + (f"  ·  {', '.join(r['tags'])}" if r["tags"] else ""))
        if r["source"]:
            lines.append(f"_{r['source']}_")
        if r["status"] != "graded":
            lines += [f"- **{r['status']}**: {r.get('error')}", ""]
            continue
        for t in r["turns"]:
            failed = [c for c in t["checks"] if not c["ok"]]
            reply = (t["reply"] or "(no reply)").replace("\n", " ⏎ ")
            lines.append(f"- **{t['say']}** → `{t['trace']['path']}`"
                         + (f" · shown {', '.join(t['shown'])}" if t["shown"] else "")
                         + (" · Pay Now" if t["cta"] else "")
                         + (" · scripted" if t["scripted"] else f" · {_ms(t['trace']['latency_ms'])}"))
            lines.append(f"  > {reply[:400]}{'…' if len(reply) > 400 else ''}")
            for f in failed:
                lines.append(f"  - ❌ `{f['name']}` {f['detail']}")
            if t.get("judge"):
                lines.append(f"  - {'✅' if t['judge']['ok'] else '❌'} judge: {t['judge']['detail']}")
        lines.append("")
    return "\n".join(lines)


def _ms(value: Optional[float]) -> str:
    return "-" if value is None else f"{value / 1000:.1f} s"


def _usd(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:.5f}"
