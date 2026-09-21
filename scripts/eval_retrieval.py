"""Measure semantic retrieval against a small, repeatable query set.

This evaluates the Chroma results directly rather than the LLM response, so
ranking and metadata-filter regressions remain visible when the LLM is absent.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import chromadb
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "backend" / ".env")

from backend.bot.chat import _build_where  # noqa: E402
from common import embeddings  # noqa: E402


@dataclass(frozen=True)
class EvaluationCase:
	query: str
	categories: tuple[str, ...] = ()
	gender: str | None = None
	max_price: float | None = None


CASES = (
	EvaluationCase("jacket to wear over my kurta", categories=("Jacket",)),
	EvaluationCase("saree for office", categories=("Saree",), gender="Women"),
	EvaluationCase("saree under 3000", categories=("Saree",), gender="Women", max_price=3000),
	EvaluationCase("sherwani for my wedding", categories=("Sherwani",), gender="Men"),
	EvaluationCase("me and my wife need wedding outfits"),
	EvaluationCase("dupatta under 2000", categories=("Dupatta",), gender="Women", max_price=2000),
	EvaluationCase("men's kurta for a festival", categories=("Kurta",), gender="Men"),
	EvaluationCase("women's lehenga for a wedding", categories=("Lehenga",), gender="Women"),
	EvaluationCase("office kurta under 2500", categories=("Kurta",), max_price=2500),
	EvaluationCase("bandhgala for a formal event", categories=("Jacket",), gender="Men"),
)


def _collection():
	client = chromadb.HttpClient(
		host=os.getenv("CHROMA_HOST", "localhost"),
		port=int(os.getenv("CHROMA_PORT", "8001")),
	)
	return client.get_or_create_collection(name="products")


def _matches_constraint(metadata: dict, case: EvaluationCase) -> bool:
	category_ok = not case.categories or metadata.get("category") in case.categories
	gender_ok = not case.gender or metadata.get("gender") == case.gender
	price_ok = case.max_price is None or float(metadata.get("price", 0)) <= case.max_price
	return category_ok and gender_ok and price_ok


def evaluate() -> None:
	collection = _collection()
	passed = 0

	for case in CASES:
		result = collection.query(
			query_embeddings=[embeddings.embed_one(case.query)],
			n_results=3,
			where=_build_where(case.query),
		)
		metadatas = [metadata or {} for metadata in result.get("metadatas", [[]])[0]]
		case_passed = any(_matches_constraint(metadata, case) for metadata in metadatas)
		passed += int(case_passed)
		products = ", ".join(
			f"{metadata.get('name', 'Item')} ({metadata.get('category', '?')}, "
			f"{metadata.get('gender', '?')}, Rs. {metadata.get('price', '?')})"
			for metadata in metadatas
		)
		status = "PASS" if case_passed else "FAIL"
		print(f"{status} | {case.query} | {products}")

	print(f"\nHit rate: {passed}/{len(CASES)} ({passed / len(CASES):.0%})")


if __name__ == "__main__":
	evaluate()
