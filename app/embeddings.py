import math

import httpx

from app.config import settings

JINA_EMBEDDINGS_URL = "https://api.jina.ai/v1/embeddings"
JINA_MODEL = "jina-embeddings-v3"


async def get_embedding(text: str) -> list[float]:
    if not settings.jina_api_key:
        raise RuntimeError("JINA_API_KEY is not configured")
    if not text.strip():
        raise ValueError("Cannot embed empty text")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            JINA_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": JINA_MODEL,
                "task": "retrieval.passage",
                "input": [text[:8000]],
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def get_query_embedding(text: str) -> list[float]:
    if not settings.jina_api_key:
        raise RuntimeError("JINA_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            JINA_EMBEDDINGS_URL,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": JINA_MODEL,
                "task": "retrieval.query",
                "input": [text[:2000]],
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
