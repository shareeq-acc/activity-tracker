from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- storage ----------------------------------------------------------
    data_dir: Path = Path("/data")
    rules_path: Path = Path("/app/rules.yml")

    # --- ingest -----------------------------------------------------------
    ingest_token: str = "change-me"
    min_segment_seconds: float = 2.0

    # --- display ----------------------------------------------------------
    tz: str = "UTC"

    # --- llm --------------------------------------------------------------
    default_llm_provider: str = "ollama"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"

    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen2.5:7b-instruct"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tracker.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"


settings = Settings()
