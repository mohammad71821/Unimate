import httpx

from app.ai.base import AIProvider
from app.config import settings

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(AIProvider):
    async def complete(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.groq_model,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=150.0) as client:
            response = await client.post(GROQ_URL, headers=headers, json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"Groq error {response.status_code}: {response.text}")
            data = response.json()

        return data["choices"][0]["message"]["content"]
