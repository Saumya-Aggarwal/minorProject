"""Embedding provider, shared by ingestion and retrieval.

Both sides must use the same model: vectors written by one model cannot be
searched with another. Keeping that choice in one module is what prevents the
two from silently drifting apart.

Switch with EMBEDDING_PROVIDER in backend/.env:
  local  (default) all-MiniLM-L6-v2 via ChromaDB, 384 dims, CPU, no API key
  openai           text-embedding-3-small, 1536 dims, needs OPENAI_API_KEY

Changing provider changes the vector width, so the catalog must be re-ingested:
    backend/.venv/Scripts/python scripts/ingest_catalog.py
"""

import os
from functools import lru_cache

LOCAL_MODEL = "all-MiniLM-L6-v2"
OPENAI_MODEL = "text-embedding-3-small"


def provider() -> str:
    return os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()


def model_name() -> str:
    return OPENAI_MODEL if provider() == "openai" else LOCAL_MODEL


@lru_cache(maxsize=1)
def _local_embedder():
    """Downloads ~80MB on first use, then caches under ~/.cache/chroma."""
    from chromadb.utils import embedding_functions

    return embedding_functions.DefaultEmbeddingFunction()


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings with the configured provider."""
    if not texts:
        return []

    if provider() == "openai":
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set. "
                "Set the key, or use EMBEDDING_PROVIDER=local."
            )
        response = OpenAI(api_key=api_key).embeddings.create(
            model=OPENAI_MODEL, input=texts
        )
        return [item.embedding for item in response.data]

    # The local model hands back numpy float32 arrays. list() on one of those
    # yields np.float32 scalars, which Chroma's validator rejects — tolist()
    # converts all the way down to Python floats.
    return [
        vector.tolist() if hasattr(vector, "tolist") else [float(x) for x in vector]
        for vector in _local_embedder()(texts)
    ]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]
