"""Conversation evals: play the cases in evals/cases/<brand>/ through the real bot
and grade every turn (P2). See evals/README.md for the case format.

From the repo root (Postgres up; the model's API key in backend/.env):
    backend/.venv/Scripts/python scripts/eval_conversations.py                    # kurta, first config
    ... --config gpt-oss-120b --only "cart-*" --tag checkout --repeat 3
    ... --list                       # check every case file and list the cases; no model calls
    ... --resume evals/runs/<dir>    # carry on after the daily quota ran out
    ... --save-baseline              # this run becomes evals/baselines/<brand>-<config>.json
    ... --judge                      # also grade `judge` rubrics (LLM_JUDGE_* model)
    ... --dry                        # a canned model answer: checks every case plays, costs nothing

Writes evals/runs/<time>-<brand>-<config>/ (results.jsonl, summary.json,
report.md; private: model outputs). Compares with the saved baseline when
there is one. Exit 1 on a setup error or a case that passed in the baseline
and fails now; failing cases alone are what a baseline is for.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend", ROOT / "scripts"):
    sys.path.insert(0, str(path))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ["PAYMENT_RECONCILE_SECONDS"] = "0"

import yaml  # noqa: E402

EVALS = ROOT / "evals"


def _configs() -> dict:
    return yaml.safe_load((EVALS / "configs.yaml").read_text(encoding="utf-8"))


def main() -> int:
    settings = _configs()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brand", default=next(iter(settings["brands"])))
    parser.add_argument("--config", default=next(iter(settings["configs"])), choices=list(settings["configs"]))
    parser.add_argument("--only", nargs="*", help="case id patterns (fnmatch)")
    parser.add_argument("--tag", nargs="*")
    parser.add_argument("--repeat", type=int, default=1, help="play each case N times (stability)")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the eval customers (debugging)")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry", action="store_true", help="canned model answers: check setup, no quota")
    args = parser.parse_args()

    from eval_harness.runner import ModelConfig
    config = ModelConfig(name=args.config, **settings["configs"][args.config])
    config.apply(os.environ)             # before anything reads the LLM settings

    import catalog
    import tenancy
    from db import init_db
    from eval_harness import cases as case_files
    from eval_harness import report, state
    from eval_harness.pacing import QuotaExhausted
    from eval_harness.runner import Runner

    brand = settings["brands"][args.brand]
    store_id = int(brand["store_id"])
    init_db()
    with tenancy.use_store(store_id):
        products = {p["id"]: p for p in catalog.get_all()}
    try:
        cases = case_files.load(EVALS / "cases", args.brand, products, args.only, args.tag)
    except case_files.CaseError as exc:
        print(f"Case files are invalid: {exc}")
        return 2
    if args.list:
        for case in cases:
            print(f"{case.id:42} {len(case.prelude)}+{len(case.turns)} turns  {', '.join(case.tags)}")
        print(f"\n{len(cases)} cases, all valid")
        return 0

    import llm
    if args.dry:
        from eval_harness.runner import ScriptedModel
        os.environ["LLM_CHAT_API_KEY"] = os.environ["LLM_API_KEY"] = "dry-run"
        llm.client = lambda role="chat", timeout=12.0: ScriptedModel([])
        args.save_baseline, args.judge = False, False
        config.tpm = None                # nothing to pace: no provider is called
    if not llm.available("chat"):
        print("No API key for the chat model: set it in backend/.env (or the config's api_key_env).")
        return 2

    run_dir = args.resume.resolve() if args.resume else EVALS / "runs" / (
        f"{datetime.now().strftime('%Y%m%d-%H%M')}-{args.brand}-{'dry' if args.dry else args.config}")
    run_dir.mkdir(parents=True, exist_ok=True)
    results_file = run_dir / "results.jsonl"
    done = {}
    if results_file.exists():
        for line in results_file.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["status"] != "quota":
                done[row["id"]] = row
    print(f"{len(cases)} cases × {args.repeat} · {args.brand} · {config.name} ({config.model})"
          + (f" · resuming, {len(done)} done" if done else ""))

    judge = None
    if args.judge:
        from eval_harness import judge as judging
        if not llm.available("judge"):
            print("--judge needs LLM_JUDGE_MODEL / LLM_JUDGE_API_KEY in backend/.env")
            return 2
        if judging.same_family(config.model):
            print(f"WARNING: the judge ({llm.config('judge').model}) is the same family as the model under "
                  "test; it will grade its own habits.")
    swept = state.sweep()
    if swept:
        print(f"(removed {swept} eval customer(s) left by an interrupted run)")

    runner = Runner(config=config, store_id=store_id, keep=args.keep)
    if args.judge:
        runner.judge = judging.make(runner.paced)
    results = list(done.values())
    stopped = ""
    with results_file.open("a", encoding="utf-8") as out:
        for case in cases:
            for repeat in range(1, args.repeat + 1):
                rid = case.id if repeat == 1 else f"{case.id}#{repeat}"
                if rid in done:
                    continue
                if stopped:
                    results.append({"id": rid, "case": case.id, "file": case.file, "tags": case.tags,
                                    "source": case.source, "status": "quota", "error": stopped, "turns": []})
                    continue
                try:
                    result = runner.run(case, repeat)
                except QuotaExhausted as exc:
                    stopped = str(exc)
                    print(f"  ⏸ quota exhausted at {rid}: {stopped[:300]}")
                    results.append({"id": rid, "case": case.id, "file": case.file, "tags": case.tags,
                                    "source": case.source, "status": "quota", "error": stopped, "turns": []})
                    continue
                failed = [f"{c['name']}" for t in result["turns"] for c in t["checks"] if not c["ok"]]
                mark = "PASS" if result["passed"] else ("SETUP" if result["status"] != "graded" else "FAIL")
                print(f"  {mark:5} {rid:42} {', '.join(dict.fromkeys(failed))[:90] or result.get('error', '')[:90]}")
                out.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                out.flush()
                results.append(result)

    meta = {"brand": args.brand, "config": config.name, "model": config.model,
            "reasoning_effort": config.reasoning_effort, "started": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "run_dir": str(run_dir.relative_to(ROOT)).replace("\\", "/"), "cases_dir": f"evals/cases/{args.brand}",
            "usd_inr": settings.get("usd_inr"), "waited_s": runner.pacer.waited_s,
            "rate_limited": runner.pacer.rate_limited}
    order = {c.id: n for n, c in enumerate(cases)}
    results.sort(key=lambda r: (order.get(r["case"], 1e9), r["id"]))
    summary = report.summarise(results, meta)
    baseline_file = EVALS / "baselines" / f"{args.brand}-{config.name}.json"
    diff = None
    if baseline_file.exists() and not args.save_baseline and not args.dry:
        diff = report.compare(summary, json.loads(baseline_file.read_text(encoding="utf-8")))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text(report.markdown(summary, results, diff), encoding="utf-8")

    c = summary["cases"]
    print(f"\nCases passed {c['passed']}/{c['graded']} · checks {summary['checks']['pass_rate']} · "
          f"setup errors {c['setup_errors']} · not run {c['not_run']}")
    print(f"Report: {run_dir / 'report.md'}")
    if stopped:
        print(f"Quota ran out. Resume later with:  --config {config.name} --resume {run_dir}")
    if args.save_baseline:
        if c["not_run"] or c["setup_errors"]:
            print("Not saved as the baseline: some cases did not run cleanly.")
        else:
            baseline_file.parent.mkdir(parents=True, exist_ok=True)
            keep = {k: v for k, v in summary.items() if k != "run_dir"}
            baseline_file.write_text(json.dumps(keep, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"Baseline saved: {baseline_file.relative_to(ROOT)}")
    if diff:
        print(f"Against the baseline: {len(diff['regressions'])} regression(s), {len(diff['fixed'])} fixed")
    return 1 if c["setup_errors"] or (diff and diff["regressions"]) else 0


if __name__ == "__main__":
    sys.exit(main())
