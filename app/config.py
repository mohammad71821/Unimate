from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    ai_provider: str = "openrouter"
    ai_max_concurrent: int = 3
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    jina_api_key: str = ""

    bot_shared_secret: str = ""
    telegram_bot_token: str = ""
    bale_bot_token: str = ""

    admin_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
