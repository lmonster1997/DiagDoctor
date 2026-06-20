"""Application configuration via Pydantic Settings."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "DiagDoctor API"
    app_version: str = "0.1.0"
    debug: bool = False
    port: int = 8000

    # --- LLM ---
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096

    # --- Embedding ---
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")

    # --- Loki / Tempo (for evidence collection tools) ---
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"

    # --- OpenTelemetry ---
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "doctor-api"

    # --- CORS ---
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # --- Checkpointer ---
    checkpoint_db_path: str = "data/checkpoints.db"

    # --- Paths ---
    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


settings = Settings()
