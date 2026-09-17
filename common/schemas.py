"""Shared schemas — the contract between the FastAPI backend and the RAG pipeline.

ChromaDB does not validate metadata, so a renamed or missing key surfaces as a
silent default rather than an error. Normalizing through ProductMatch at the
boundary is what turns that into a visible failure.

Two key styles exist in the codebase today: the catalog/Chroma style
(id/price/description) and the contract style (product_id/price_inr/
rich_description). `from_raw` accepts either, so neither side has to change
first.
"""

from typing import Any, Union

from pydantic import BaseModel, Field


class ProductMatch(BaseModel):
    """A single catalog product retrieved for a user query."""

    product_id: str
    name: str
    price_inr: float
    rich_description: str = ""

    @classmethod
    def from_raw(cls, raw: Union["ProductMatch", dict[str, Any]]) -> "ProductMatch":
        """Build from either key style, or pass through an existing ProductMatch."""
        if isinstance(raw, ProductMatch):
            return raw
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()

        return cls(
            product_id=str(raw.get("product_id") or raw.get("id") or ""),
            name=str(raw.get("name") or "Unknown item"),
            price_inr=float(raw.get("price_inr") or raw.get("price") or 0),
            rich_description=str(
                raw.get("rich_description") or raw.get("description") or ""
            ),
        )


class ConversationTurn(BaseModel):
    """What the assistant showed on the previous turn, stored in sessions."""

    query: str
    products: list[ProductMatch] = Field(default_factory=list)
