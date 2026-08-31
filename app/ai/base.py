from abc import ABC, abstractmethod


class AIProvider(ABC):
    @abstractmethod
    async def complete(self, prompt: str, system: str | None = None, json_mode: bool = False) -> str:
        """Send a prompt to the LLM and return the text response."""
        raise NotImplementedError
