import httpx

from app.ai.base import AIProvider
from app.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(AIProvider):
    async def complete(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.openrouter_model,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=150.0) as client:
            response = await client.post(OPENROUTER_URL, headers=headers, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text}")
            data = response.json()

        return data["choices"][0]["message"]["content"]
