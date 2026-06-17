"""Application configuration via Pydantic Settings."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_name: str = "TaskFlow API"
    app_version: str = "0.1.0"
    debug: bool = False

    # --- Database ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/taskflow"

    # --- JWT / Auth ---
    jwt_secret: str = "change-me-in-production-use-a-strong-random-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # --- OpenTelemetry ---
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "demo-backend"

    # --- CORS ---
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # --- Paths ---
    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


settings = Settings()
