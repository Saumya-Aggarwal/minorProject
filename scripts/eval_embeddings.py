"""Compare embedding models on our own data, and derive their two thresholds.

For each model named (default: minilm gemma) the catalogue is re-embedded in
place (db.init_db does that when EMBEDDING_PROVIDER changes; ~2 s for 84
products), then:

  1. eval      the 46 cases of eval_retrieval.py through the whole engine
  2. meaning   precision@5 of the vector ranking ALONE (no filters) on every
               case that names a category or a gender: how often the nearest
               five really are what was asked. The filters hide a weak model
               in (1); this is where models differ.
  3. hinglish  the same measure on Hindi-English phrasings of those requests
  4. cut-off   nearest distance for real requests vs nonsense and off-topic
               text -> MAX_DISTANCE (bot/retrieval.py)
  5. similar   cosine of question pairs that should / should not share an
               owner rating -> SIMILAR (training.py)

Thresholds are printed, not written: copy the chosen model's pair into
common/embeddings.THRESHOLDS. At the end the catalogue is re-embedded with
the provider in backend/.env again.

Run from the repo root:
    backend/.venv/Scripts/python scripts/eval_embeddings.py [minilm gemma openai]
"""

import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "backend" / ".env")

import catalog  # noqa: E402
import eval_retrieval  # noqa: E402
import training  # noqa: E402
from bot import retrieval  # noqa: E402
from common import embeddings  # noqa: E402
from db import init_db  # noqa: E402

# Hindi-English versions of eval cases: (query, categories, gender)
HINGLISH = (
    ("shaadi ke liye sherwani", ("Sherwani",), "Men"),
    ("office ke liye saree", ("Saree",), "Women"),
    ("meri behen ki shaadi ke liye lehenga", ("Lehenga",), "Women"),
    ("biwi ke liye kurti chahiye", ("Kurti", "Salwar Suit"), "Women"),
    ("bhai ki haldi ke liye kurta", ("Kurta", "Kurta Set"), "Men"),
    ("dulhe ke liye jutti", ("Footwear",), "Men"),
    ("kurte ke upar pehenne wali jacket", ("Nehru Jacket", "Waistcoat"), None),
    ("garba ke liye chaniya choli", ("Lehenga",), "Women"),
    ("lehenge ke saath matching potli bag", ("Bags",), "Women"),
    ("baraat ke liye safa", ("Headwear",), "Men"),
)

# Must return nothing when asked open-endedly
NONSENSE = ("asdfgh qwerty", "zzqqxx vbnm", "what is the capital of france",
            "write python code for 2+2", "is it going to rain tomorrow", "hello how are you",
            "my phone screen is broken", "best biryani near me")

# (a, b, should they share an owner rating?)
PAIRS = (
    ("saree for office", "office saree for work", True),
    ("sherwani for my wedding", "wedding sherwani for me", True),
    ("haldi outfit for my brother", "something yellow for my brother's haldi", True),
    ("jacket to wear over my kurta", "nehru jacket over a kurta", True),
    ("kurta under 2k", "kurta below 2000", True),
    ("lehenga for my sister's sangeet", "sangeet lehenga for my sister", True),
    ("shoes to go with my sherwani", "footwear for my sherwani", True),
    ("sherwani for my wedding", "saree for office", False),
    ("sherwani for my wedding", "kurti for office", False),
    ("jacket to wear over my kurta", "kurti for office", False),
    ("saree for office", "saree for a wedding reception", False),
    ("haldi outfit for my brother", "haldi outfit for my sister", False),
    ("shoes to go with my sherwani", "sherwani for my wedding", False),
    ("lehenga under 5000", "sherwani under 5000", False),
)


def fits(product, categories, gender) -> bool:
    return ((not categories or product["category"] in categories)
            and (not gender or product["gender"] == gender))


def precision_at_5(query, categories, gender) -> float:
    ranked, semantic = retrieval.rank(query)
    if not semantic:
        raise RuntimeError("vector search unavailable")
    top = [p for p, _ in ranked[:5]]
    return sum(fits(p, categories, gender) for p in top) / 5


def best_cut(low: list[float], high: list[float]) -> tuple[float, int]:
    """Threshold that best separates `low` (should pass) from `high` (should not)."""
    points = sorted(set(low) | set(high))
    candidates = [(a + b) / 2 for a, b in zip(points, points[1:])] or points
    scored = [(sum(x <= t for x in low) + sum(x > t for x in high), t) for t in candidates]
    right, threshold = max(scored, key=lambda pair: (pair[0], -abs(pair[1] - sum(points) / len(points))))
    return threshold, right


def measure(provider: str) -> dict:
    os.environ["EMBEDDING_PROVIDER"] = provider
    catalog.invalidate()
    init_db()                                   # re-embeds the catalogue with this model
    with contextlib.redirect_stdout(io.StringIO()):
        cases_passed = sum(eval_retrieval.judge(c, retrieval.search(c.query))[0] for c in eval_retrieval.CASES)
    named = [c for c in eval_retrieval.CASES if (c.categories or c.gender) and not c.empty and not c.both_genders]
    meaning = sum(precision_at_5(c.query, c.categories, c.gender) for c in named) / len(named)
    hinglish = sum(precision_at_5(q, cats, g) for q, cats, g in HINGLISH) / len(HINGLISH)

    nearest = lambda q: retrieval.rank(q)[0][0][1]      # noqa: E731
    real = [nearest(c.query) for c in eval_retrieval.CASES if not c.empty]
    junk = [nearest(q) for q in NONSENSE]
    max_distance, cut_right = best_cut(real, junk)

    sims = [(training._cosine(embeddings.embed_similarity(a), embeddings.embed_similarity(b)), same)
            for a, b, same in PAIRS]
    # best_cut keeps values <= t on the "low" side: negated, the similar pairs are the low ones
    neg_t, sim_right = best_cut([-s for s, same in sims if same], [-s for s, same in sims if not same])
    return {
        "model": embeddings.model_name(), "cases": cases_passed, "meaning": meaning, "hinglish": hinglish,
        "max_distance": max_distance, "cut_right": cut_right, "cut_total": len(real) + len(junk),
        "real_max": max(real), "junk_min": min(junk),
        "similar": -neg_t, "sim_right": sim_right, "sim_total": len(sims), "sims": sims,
    }


def main() -> int:
    providers = sys.argv[1:] or ["minilm", "gemma"]
    configured = os.getenv("EMBEDDING_PROVIDER", "minilm")
    results = []
    try:
        for provider in providers:
            print(f"Measuring {provider}...", flush=True)
            results.append(measure(provider))
    finally:
        os.environ["EMBEDDING_PROVIDER"] = configured
        catalog.invalidate()
        init_db()

    print(f"\n{'model':<24}{'eval':>7}{'meaning@5':>11}{'hinglish@5':>12}"
          f"{'MAX_DISTANCE':>14}{'separates':>11}{'SIMILAR':>9}{'separates':>11}")
    for r in results:
        print(f"{r['model']:<24}{r['cases']:>4}/46{r['meaning']:>11.0%}{r['hinglish']:>12.0%}"
              f"{r['max_distance']:>14.3f}{r['cut_right']:>6}/{r['cut_total']:<4}"
              f"{r['similar']:>9.3f}{r['sim_right']:>6}/{r['sim_total']}")
    for r in results:
        print(f"\n{r['model']}: farthest real request {r['real_max']:.3f}, nearest nonsense {r['junk_min']:.3f}")
        for (a, b, same), (sim, _) in zip(PAIRS, r["sims"]):
            wrong = (sim >= r["similar"]) != same
            print(f"  {sim:.3f} {'same' if same else 'diff'} {'<- WRONG' if wrong else '':8} {a} | {b}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
