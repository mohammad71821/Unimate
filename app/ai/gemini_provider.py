import httpx

from app.ai.base import AIProvider
from app.config import settings

GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(AIProvider):
    async def complete(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        url = GEMINI_URL_TEMPLATE.format(model=settings.gemini_model)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"] = {"responseMimeType": "application/json"}

        async with httpx.AsyncClient(timeout=150.0) as client:
            response = await client.post(url, params={"key": settings.gemini_api_key}, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"Gemini error {response.status_code}: {response.text}")
            data = response.json()

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Gemini returned unexpected response shape: {data}") from e
