"""The P2 eval harness grades what it should, and cleans up after itself.

No quota: every model answer is scripted or a fake at llm.client. Checks:
  * every case file in evals/cases loads and is valid
  * the code checks: claims, sizes, amounts, fragments, fit, cart diffs
  * whole cases through the real routing: a right answer passes, a wrong one
    fails the right check, a broken prelude is a setup error, not a grade
  * pacing: a per-minute 429 is waited out without the bot noticing (no
    fallback, the wait left out of latency); a per-day 429 stops the run
  * eval customers and their traces are removed

Run from the repo root (Postgres up). Part of run_all_tests.py.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend", ROOT / "scripts"):
    sys.path.insert(0, str(path))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")
os.environ.update({"PAYMENT_RECONCILE_SECONDS": "0", "WHATSAPP_APP_SECRET": "", "LLM_API_KEY": "fake-key"})
for name in [k for k in os.environ if k.startswith("LLM_CHAT_")]:
    del os.environ[name]

import httpx  # noqa: E402
import openai  # noqa: E402
from sqlalchemy import text  # noqa: E402

import catalog  # noqa: E402
import llm  # noqa: E402
from db import init_db, session_scope  # noqa: E402
from eval_harness import cases as case_files  # noqa: E402
from eval_harness import checks, report, state  # noqa: E402
from eval_harness.pacing import QuotaExhausted  # noqa: E402
from eval_harness.runner import ModelConfig, Runner, _completion  # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


# --- a fake provider for the "real" model ----------------------------------------------

def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://fake.invalid/v1/chat/completions"))


class Provider:
    """llm.client stand-in: answers or raises, in order."""

    def __init__(self) -> None:
        self.steps: list = []

    def _next(self):
        step = self.steps.pop(0) if self.steps else {"text": "(nothing scripted)"}
        if isinstance(step, BaseException):
            raise step
        return _completion(step)

    @property
    def chat(self):
        provider = self

        class Raw:
            def create(self, **_):
                parsed = provider._next()
                return type("R", (), {"parse": lambda self: parsed, "headers": httpx.Headers({})})()

        class Completions:
            with_raw_response = Raw()

            def create(self, **_):
                return provider._next()

        return type("C", (), {"completions": Completions()})()


PROVIDER = Provider()
llm.client = lambda role="chat", timeout=12.0: PROVIDER


def per_minute_429():
    return openai.RateLimitError("Error code: 429 - Rate limit reached for model on tokens per minute (TPM): "
                                 "Limit 8000, Used 7900. Please try again in 1.5s.", response=_response(429), body=None)


def per_day_429():
    return openai.RateLimitError("Error code: 429 - Rate limit reached for model on tokens per day (TPD): "
                                 "Limit 200000. Please try again in 7m12s.", response=_response(429), body=None)


# --- unit checks ------------------------------------------------------------------------

def snap(cart=(), selected=None, shown=(), orders=()):
    cart = [dict(c) for c in cart]
    for line in cart:
        line.setdefault("qty", 1)
        line.setdefault("line_total", 0)
    return {"cart": cart, "cart_total": sum(l["line_total"] for l in cart), "cart_units": sum(l["qty"] for l in cart),
            "orders": list(orders), "session": {"selected": selected, "shown": list(shown), "pending_address": None,
                                                "reorder": None, "messages": 0},
            "profile": {"gender": None, "notes": []}, "address": None, "hidden": []}


def turn(reply, before=None, after=None, say="", shown=(), trace=None):
    return checks.Turn(say=say, reply={"text": reply, "product_ids": list(shown)}, trace=trace or {"path": "assistant"},
                       before=before or snap(), after=after or before or snap(), products=PRODUCTS, customer_said=say)


def failed_names(t, expect=None):
    return {c.name for c in checks.grade(t, expect or {}, []) if not c.ok}


def unit_checks() -> None:
    print("code checks")
    added = snap(cart=[{"product": "EW052", "size": "UK 11", "line_total": 1999}])
    check("a promised add with no cart change fails claims_backed",
          "claims_backed" in failed_names(turn("Sure, I'll add the Silver Paisley Mojari in UK 11.", snap(), snap())))
    check("an add that happened passes claims_backed",
          "claims_backed" not in failed_names(turn("Added the Silver Paisley Mojari (UK 11).", snap(), added)))
    check("'Removed 2' when nothing left the cart fails",
          "claims_backed" in failed_names(turn("Removed 2 Aqua Chanderi Saree from your cart.", added, added)))
    check("talking about a Pay Now button that was not sent fails",
          "pay_claim_backed" in failed_names(turn("Here's your secure Pay Now button. Just tap it!")))
    clutch = snap(selected="EW080", shown=["EW080"])
    check("invented sizes for a free-size clutch fail sizes_real",
          "sizes_real" in failed_names(turn("Lovely gift. Which size would you like? (Sizes: S, M, L, XL)", clutch)))
    check("'in Medium' for a free-size clutch fails sizes_real",
          "sizes_real" in failed_names(turn("I'll add the Mint Embellished Box Clutch in Medium.", clutch)))
    check("the s of It’s is not a size S",
          "sizes_real" not in failed_names(turn("It’s one-size-fits-all, so there is no size to pick.", clutch)))
    sherwani = snap(selected="EW042")
    check("the real sizes of a sherwani pass sizes_real",
          "sizes_real" not in failed_names(turn("Which size? It comes in 38, 40, 42, 44 and 46.", sherwani)))
    check("S-XXL offered for a sherwani fails sizes_real",
          "sizes_real" in failed_names(turn("Which size would you like (e.g., S, M, L, XL, XXL)?", sherwani)))
    check("a price the model added up itself fails amounts_real",
          "amounts_real" in failed_names(turn("Your total is Rs 17,498.", added, added)))
    check("a catalogue price and a typed budget pass amounts_real",
          "amounts_real" not in failed_names(turn("Under Rs 7k: this one is Rs 1,899.", say="around 7k")))
    check("'Added the.' fails no_fragments", "no_fragments" in failed_names(turn("Added the.\n\n1. Mint clutch")))
    check("'Here are options for your sister's haldi. The.' fails no_fragments",
          "no_fragments" in failed_names(turn("Here are some options for your sister's haldi. The.")))
    check("an ordinary sentence passes no_fragments",
          "no_fragments" not in failed_names(turn("These suit a haldi in the morning. Want more?")))
    check("a code snippet fails no_code", "no_code" in failed_names(turn("Sure!\n```python\nprint(2+2)\n```")))
    check("the cart shown twice fails cart_shown_once",
          "cart_shown_once" in failed_names(turn("Your cart:\n1. Suit\n\nYour cart:\n1. Suit")))
    check("a fallback to plain search fails no_fallback",
          "no_fallback" in failed_names(turn("Here you go", trace={"path": "search",
                                                                   "guards": [{"name": "fallback", "reason": "x"}]})))
    women = turn("Here you are", shown=["EW063", "EW039"])
    check("a women's lehenga shown for 'only male options' fails shown_fit",
          "shown_fit" in failed_names(women, {"shown_fit": {"gender": "Men"}}))
    check("shown_fit names the product that does not fit",
          any("EW063" in c.detail for c in checks.expected(women, {"shown_fit": {"gender": "Men"}})))
    check("colour fit reads the product's colour, not only its name",
          not checks.misfit(PRODUCTS["EW001"], {"colour": "maroon"})          # "Brocade Silk Kurta"
          and "White" in checks.misfit(PRODUCTS["EW083"], {"colour": "black"}),
          (checks.misfit(PRODUCTS["EW001"], {"colour": "maroon"}), checks.misfit(PRODUCTS["EW083"], {"colour": "black"})))
    remove = turn("Done", before=snap(cart=[{"product": "EW071", "size": "Free Size", "qty": 3}]),
                  after=snap(cart=[{"product": "EW071", "size": "Free Size", "qty": 1}]))
    check("an exact cart expectation compares quantities",
          not failed_names(remove, {"cart": [{"product": "EW071", "size": "Free Size", "qty": 1}]}) - {"replied"}
          and "cart" in failed_names(remove, {"cart": [{"product": "EW071", "qty": 2}]}))
    check("a path pattern with * matches a family of exits",
          "path" not in failed_names(turn("x", trace={"path": "lookup:address"}), {"path": "lookup*"}))


def case_files_valid() -> None:
    print("case files")
    try:
        loaded = case_files.load(ROOT / "evals" / "cases", "kurta", PRODUCTS)
        check(f"all {len(loaded)} kurta cases load and are valid", len(loaded) >= 50, len(loaded))
    except case_files.CaseError as exc:
        check("all kurta cases load and are valid", False, exc)
    bad = {"id": "x", "turns": [{"say": "hi", "expect": {"shows_any": ["EW999"]}}]}
    try:
        case_files._case(bad, "kurta", "t.yaml", PRODUCTS)
        check("a product the store does not sell is refused", False)
    except case_files.CaseError:
        check("a product the store does not sell is refused", True)
    try:
        case_files._case({"id": "x", "turns": [{"say": "hi", "expect": {"show": True}}]}, "kurta", "t.yaml", PRODUCTS)
        check("a misspelt expectation is refused", False)
    except case_files.CaseError:
        check("a misspelt expectation is refused", True)


# --- whole cases -------------------------------------------------------------------------

def make_case(raw):
    return case_files._case(raw, "kurta", "test.yaml", PRODUCTS)


TEST_CASES = ("only-male", "checkout", "photo", "broken", "placeholders", "pace")


def eval_users() -> int:
    """This test's customers still in the database (a baseline may be running alongside)."""
    phones = [state.phone_for("harness-test", case, 1) for case in TEST_CASES]
    with session_scope() as db:
        return db.execute(text("SELECT count(*) FROM users WHERE whatsapp_number = ANY(:p)"),
                          {"p": phones}).scalar()


def whole_cases(runner: Runner) -> None:
    print("whole cases")
    only_male = {"id": "only-male", "prelude": [{"say": "something for a night wedding", "model": [
        {"send_reply": {"message": "A few for a night wedding.", "product_ids": ["EW063", "EW039"]}}]}],
        "turns": [{"say": "only male options", "expect": {"shows": True, "shown_fit": {"gender": "Men"}}}]}

    PROVIDER.steps = [{"send_reply": {"message": "Men's picks for the evening.", "product_ids": ["EW039", "EW006"]}}]
    good = runner.run(make_case(only_male))
    check("a right answer passes", good["passed"], json.dumps(good, default=str)[:600])
    t = good["turns"][0] if good["turns"] else {}
    check("the graded turn went through the assistant, by the model under test",
          t.get("trace", {}).get("path") == "assistant" and not t.get("scripted"), t.get("trace", {}).get("path"))

    PROVIDER.steps = [{"send_reply": {"message": "Here you go.", "product_ids": ["EW063", "EW019"]}}]
    bad = runner.run(make_case(only_male))
    fails = {c["name"] for tt in bad["turns"] for c in tt["checks"] if not c["ok"]}
    check("women's wear for 'only male options' fails shown_fit, and only that", fails == {"shown_fit"}, fails)

    checkout = {"id": "checkout", "given": {"address": "default", "cart": [{"product": "EW006", "size": "42"}]},
                "turns": [{"say": "CHECKOUT", "expect": {"path": "command:checkout", "cta": True, "order_placed": True}}]}
    done = runner.run(make_case(checkout))
    check("CHECKOUT with a cart and an address passes (code path, no model)", done["passed"],
          [c for tt in done["turns"] for c in tt["checks"] if not c["ok"]] or done.get("error"))

    photo = runner.run(make_case({"id": "photo", "turns": [{"media": "image"}]}))
    names = {c["name"]: c["ok"] for tt in photo["turns"] for c in tt["checks"]}
    check("a photo is traced as unsupported and fails 'replied' (P2 quick fix)",
          names.get("replied") is False and photo["turns"][0]["trace"]["path"] == "unsupported", names)

    broken = {"id": "broken", "prelude": [{"say": "EW045", "model": [{"send_reply": {"message": "unused"}}]}],
              "turns": [{"say": "hi"}]}
    result = runner.run(make_case(broken))
    check("a prelude that does not play as written is a setup error, not a grade",
          result["status"] == "setup_error" and not result["passed"], result.get("error"))

    scripted = {"id": "placeholders", "given": {"orders": [{"items": [{"product": "EW045", "size": "XXL"}]}]},
                "turns": [{"say": "#{order1} show full details of that order",
                           "model": [{"send_reply": {"message": "Order #{order1}: the Tangerine kurta."}}],
                           "expect": {"reply_has": ["#{order1}"]}}]}
    filled = runner.run(make_case(scripted))
    said = filled["turns"][0]["say"] if filled["turns"] else ""
    check("{order1} is filled in before the turn runs", filled["passed"] and "{" not in said, said)

    check("every eval customer was removed", eval_users() == 0, eval_users())


def pacing(runner: Runner) -> None:
    print("pacing")
    case = make_case({"id": "pace", "turns": [{"say": "saree for office", "expect": {"shows": True}}]})
    PROVIDER.steps = [per_minute_429(), {"send_reply": {"message": "Office sarees.", "product_ids": ["EW021"]}}]
    waited = runner.pacer.waited_s
    result = runner.run(case)
    tr = result["turns"][0]["trace"] if result["turns"] else {}
    check("a per-minute 429 is waited out and the bot never sees it",
          result["passed"] and not tr.get("guards"), [c for t in result["turns"] for c in t["checks"] if not c["ok"]])
    check("the wait is recorded", runner.pacer.waited_s - waited >= 1.5, runner.pacer.waited_s - waited)
    check("the wait is left out of the turn's latency", tr.get("latency_ms", 99999) < 1500, tr.get("latency_ms"))
    times = [c["ms"] for c in tr.get("llm_calls", [])]
    check("each model call's time leaves out only waits during it (never negative)",
          times and all(0 <= ms < 1500 for ms in times), times)

    PROVIDER.steps = [per_day_429()]
    try:
        runner.run(case)
        check("a per-day 429 stops the run", False)
    except QuotaExhausted:
        check("a per-day 429 stops the run", True)
    check("the customer of the stopped case was removed", eval_users() == 0, eval_users())
    PROVIDER.steps = []


def report_works() -> None:
    print("report")
    results = [{"id": "a", "case": "a", "file": "f", "tags": ["x"], "source": "", "status": "graded", "passed": True,
                "turns": [{"say": "hi", "scripted": False, "reply": "Hello", "shown": [], "cta": False, "judge": None,
                           "checks": [{"name": "replied", "ok": True, "detail": "", "universal": True}],
                           "trace": {"path": "greeting", "guards": [], "llm_calls": [], "tokens_in": 0, "tokens_out": 0,
                                     "cost_usd": 0, "latency_ms": 10, "wa_messages": 1}}]},
               {"id": "b", "case": "b", "file": "f", "tags": ["x"], "source": "", "status": "quota", "turns": []}]
    summary = report.summarise(results, {"brand": "kurta", "config": "t", "usd_inr": 88})
    check("the summary counts passed and not-run cases",
          summary["cases"]["passed"] == 1 and summary["cases"]["not_run"] == 1, summary["cases"])
    diff = report.compare({"case_results": {"a": False, "c": True}}, {"case_results": {"a": True, "c": False}})
    check("a case that passed in the baseline and fails now is a regression",
          diff["regressions"] == ["a"] and diff["fixed"] == ["c"], diff)
    check("the Markdown report renders", "Cases passed: 1/1" in report.markdown(summary, results))


def main() -> int:
    global PRODUCTS
    init_db()
    PRODUCTS = {p["id"]: p for p in catalog.get_all()}
    state.sweep()
    unit_checks()
    case_files_valid()
    config = ModelConfig(name="harness-test", model="fake-model", tpm=None)
    config.apply(os.environ)
    runner = Runner(config=config, store_id=1, log=lambda line: None)
    try:
        whole_cases(runner)
        pacing(runner)
    finally:
        runner.restore()
        state.sweep()
    report_works()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


PRODUCTS: dict = {}

if __name__ == "__main__":
    sys.exit(main())
