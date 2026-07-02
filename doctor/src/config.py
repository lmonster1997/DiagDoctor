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

    # Role-specific LLM overrides (fall back to llm_model if empty)
    llm_triage_model: str = ""
    llm_triage_temperature: float = 0.1
    llm_triage_max_tokens: int = 2048
    llm_specialist_model: str = ""
    llm_specialist_temperature: float = 0.1
    llm_specialist_max_tokens: int = 4096

    # LLM-as-Judge（评测专用，建议用最强模型如 gpt-4o）
    # 独立 API key / base_url 可选——不设置则复用 llm_api_key / llm_base_url
    llm_judge_api_key: SecretStr = SecretStr("")
    llm_judge_base_url: str = ""
    llm_judge_model: str = ""  # fallback: llm_specialist_model → llm_model
    llm_judge_temperature: float = 0.0  # judge 需要确定性
    llm_judge_max_tokens: int = 1024  # judge 只需输出分数 + 一句话理由

    # DeepSeek thinking mode 开关（仅对 deepseek 模型生效）
    # false = 关掉思考模式，agent 工具调用更稳定（推荐）
    # true  = 开启思考模式，适合复杂推理任务
    llm_deepseek_thinking: bool = False

    # --- Embedding ---
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")

    # --- Loki / Tempo (for evidence collection tools) ---
    loki_url: str = "http://127.0.0.1:3100"
    tempo_url: str = "http://127.0.0.1:3200"

    # --- Demo App Database (read-only for Doctor diagnosis) ---
    # Doctor 诊断时只做 SELECT 验证数据状态，使用只读连接。
    # 默认连接 docker-compose 中的 postgres 容器（taskflow 数据库）。
    # 正式环境应使用独立的只读账号。
    demo_db_ro_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/taskflow"
    )

    # --- Target Services ---
    # Service names as they appear in OpenTelemetry instrumentation.
    # Doctor uses these to auto-prefetch logs/traces from Loki/Tempo.
    backend_service_name: str = "demo-backend"
    frontend_service_name: str = "demo-frontend"

    # --- Ingest Pipeline Thresholds ---
    # All thresholds have sensible defaults; override via env for other apps.
    ingest_slow_span_threshold_ms: float = 200.0  # Spans slower than this flagged as slow
    ingest_n1_min_count: int = 3  # Min repeated queries to trigger N+1 detection
    ingest_n1_linear_tolerance: float = 0.3  # Max deviation for linear growth check (0-1)
    ingest_time_window_minutes: int = 5  # Trigger time ± N minutes for Loki/Tempo queries

    # --- Agent Loop ---
    agent_max_tool_calls: int = 12  # Max tool call iterations before forced termination
    agent_model_context_window: int = 128_000  # Model context window (tokens)
    agent_reserved_output_tokens: int = 4_000  # Reserved for final output
    agent_context_warning_ratio: float = 0.6  # Start degradation at this budget usage
    agent_context_critical_ratio: float = 0.8  # Force termination at this budget usage

    # --- Tool Result Truncation ---
    # 当为 False 时，禁用所有工具结果的截断/压缩（用于调试诊断效果）。
    # 影响两处：
    #   1. context_engine.truncate_tool_result —— 入 context 前的字符上限
    #   2. observability_unified.search_observability —— 8000 字符 JSON 截断
    tool_result_truncation_enabled: bool = False

    # --- OpenTelemetry ---
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "doctor-api"

    # --- CORS ---
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # --- Langfuse (LLM observability & evaluation) ---
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_host: str = "http://localhost:3002"

    # --- Checkpointer ---
    checkpoint_db_path: str = "data/checkpoints.db"

    # --- Paths ---
    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


settings = Settings()
