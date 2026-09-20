from typing import Literal

import httpx2

from app.core.config import get_settings

settings = get_settings()

InputType = Literal["query", "document"]

_EMBED_PATH = "/embeddings"
# Voyage caps a request at 1000 inputs and a total token budget; stay well under both.
_BATCH_SIZE = 128


class EmbeddingConfigError(RuntimeError):
    """Embeddings are requested but no Voyage API key is configured."""


def embed(inputs: list[str], input_type: InputType, model: str | None = None) -> list[list[float]]:
    """Embeds `inputs` with Voyage AI, preserving order. Use "document" for text being indexed and "query" for
    text being searched with -- Voyage prepends a different prompt for each, which improves retrieval.
    `model` overrides the configured EMBEDDING_MODEL; vectors from different models are not comparable."""
    if not inputs:
        return []
    if not settings.voyage_api_key:
        raise EmbeddingConfigError("VOYAGE_API_KEY is not set; it is required for embedding-based retrieval")

    vectors: list[list[float]] = []
    headers = {"Authorization": f"Bearer {settings.voyage_api_key}"}
    with httpx2.Client(
        base_url=settings.voyage_base_url, headers=headers, timeout=settings.voyage_request_timeout
    ) as client:
        for start in range(0, len(inputs), _BATCH_SIZE):
            payload = {
                "input": inputs[start : start + _BATCH_SIZE],
                "model": model or settings.embedding_model,
                "input_type": input_type,
                "output_dimension": settings.embedding_dim,
            }
            response = client.post(_EMBED_PATH, json=payload)
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
    return vectors
