"""Embedding models, shared by catalogue sync, search and owner training.

Every vector stored and every vector searched must come from the same model:
products.embedding_model records which one made each row, and
catalog_sync.ensure_catalogues() re-embeds rows made by another.

EMBEDDING_PROVIDER in backend/.env:
  gemma   EmbeddingGemma-300M, 768 dims, multilingual, CPU (int8 ONNX, ~310 MB
          download on first use into ~/.cache/huggingface)
  minilm  all-MiniLM-L6-v2, 384 dims, English only, CPU (~90 MB). "local" is
          accepted as its old name.
  openai  text-embedding-3-small, 1536 dims, needs OPENAI_API_KEY

The local models run on onnxruntime + tokenizers: no PyTorch, no Chroma.

Three kinds of text are embedded, because EmbeddingGemma is trained with a
task prompt in front of each (MiniLM and OpenAI ignore the difference):
  document    a catalogue entry           embed_documents()
  query       what a customer searched    embed_query()
  similarity  a question compared with another question (owner training)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

MINILM = "all-MiniLM-L6-v2"
GEMMA = "embeddinggemma-300m"
OPENAI_MODEL = "text-embedding-3-small"

_GEMMA_REPO = "onnx-community/embeddinggemma-300m-ONNX"
_GEMMA_FILE = "onnx/model_quantized.onnx"     # int8: 310 MB instead of 1.2 GB, same ranking
_GEMMA_PROMPTS = {
    "document": "title: none | text: ",
    "query": "task: search result | query: ",
    "similarity": "task: sentence similarity | query: ",
}
_MINILM_REPO = "sentence-transformers/all-MiniLM-L6-v2"
# Chroma downloaded the identical model here; reused if present
_CHROMA_MINILM = Path.home() / ".cache" / "chroma" / "onnx_models" / MINILM / "onnx"

# (MAX_DISTANCE for open-ended searches in bot/retrieval.py, SIMILAR for
# owner training in training.py), measured per model by
# scripts/eval_retrieval.py --thresholds. Cosine distance = 1 - cosine.
THRESHOLDS: dict[str, tuple[float, float]] = {
    # Chroma's squared L2 of 1.55 on unit vectors is cosine distance 0.775
    MINILM: (0.775, 0.75),
    GEMMA: (0.775, 0.75),          # provisional until measured (P1 step 5)
    OPENAI_MODEL: (0.775, 0.75),   # never measured: no OpenAI key yet
}


def provider() -> str:
    value = os.getenv("EMBEDDING_PROVIDER", "minilm").strip().lower()
    return "minilm" if value == "local" else value


def model_name() -> str:
    return {"gemma": GEMMA, "openai": OPENAI_MODEL}.get(provider(), MINILM)


def max_distance() -> float:
    return THRESHOLDS[model_name()][0]


def similar_threshold() -> float:
    return THRESHOLDS[model_name()][1]


# --- local ONNX models ------------------------------------------------------------

@lru_cache(maxsize=1)
def _minilm() -> tuple[Any, Any]:
    import onnxruntime
    from tokenizers import Tokenizer

    folder = _CHROMA_MINILM
    if not (folder / "model.onnx").exists():
        from huggingface_hub import hf_hub_download

        model = Path(hf_hub_download(_MINILM_REPO, "onnx/model.onnx"))
        tokenizer_path = Path(hf_hub_download(_MINILM_REPO, "tokenizer.json"))
    else:
        model, tokenizer_path = folder / "model.onnx", folder / "tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=256)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=256)
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    return session, tokenizer


def _embed_minilm(texts: list[str]) -> list[list[float]]:
    """Mean-pooled, L2-normalised: the same vectors Chroma's default function made."""
    import numpy as np

    session, tokenizer = _minilm()
    vectors = []
    for start in range(0, len(texts), 32):
        encoded = tokenizer.encode_batch(texts[start:start + 32])
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        hidden = session.run(None, {"input_ids": ids, "attention_mask": mask,
                                    "token_type_ids": np.zeros_like(ids)})[0]
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(1) / np.clip(weights.sum(1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors.extend((pooled / np.clip(norms, 1e-12, None)).astype(np.float32).tolist())
    return vectors


@lru_cache(maxsize=1)
def _gemma() -> tuple[Any, Any]:
    import onnxruntime
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    model = hf_hub_download(_GEMMA_REPO, _GEMMA_FILE)
    hf_hub_download(_GEMMA_REPO, _GEMMA_FILE + "_data")      # weights, next to the graph
    tokenizer = Tokenizer.from_file(hf_hub_download(_GEMMA_REPO, "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(pad_id=tokenizer.token_to_id("<pad>") or 0, pad_token="<pad>")
    session = onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])
    return session, tokenizer


def _embed_gemma(texts: list[str]) -> list[list[float]]:
    import numpy as np

    session, tokenizer = _gemma()
    vectors = []
    for start in range(0, len(texts), 16):
        encoded = tokenizer.encode_batch(texts[start:start + 16])
        inputs = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                  "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64)}
        # Outputs: token states, then the pooled + projected sentence embedding
        pooled = session.run(None, inputs)[1]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        vectors.extend((pooled / np.clip(norms, 1e-12, None)).astype(np.float32).tolist())
    return vectors


# --- public -------------------------------------------------------------------------

def _embed(texts: list[str], kind: str) -> list[list[float]]:
    if not texts:
        return []
    name = provider()
    if name == "openai":
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set.")
        response = OpenAI(api_key=api_key).embeddings.create(model=OPENAI_MODEL, input=texts)
        return [item.embedding for item in response.data]
    if name == "gemma":
        return _embed_gemma([_GEMMA_PROMPTS[kind] + text for text in texts])
    return _embed_minilm(texts)


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Catalogue entries, as stored in products.embedding."""
    return _embed(texts, "document")


def embed_query(text: str) -> list[float]:
    """A customer's search, compared against documents."""
    return _embed([text], "query")[0]


def embed_similarity(text: str) -> list[float]:
    """A question compared with other questions (owner training)."""
    return _embed([text], "similarity")[0]
