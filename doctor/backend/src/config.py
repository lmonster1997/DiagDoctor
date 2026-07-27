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
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096

    # Role-specific LLM overrides (fall back to llm_model if empty)
    llm_specialist_model: str = ""
    llm_specialist_temperature: float = 0.0
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
    embedding_dimensions: int = 1024  # must match qdrant_client.VECTOR_SIZE
    # DashScope (Alibaba) OpenAI-compatible API key. When embedding_base_url +
    # this key are set, embedding.py routes to the API exclusively (no silent
    # fallback -- mixing embedders corrupts the vector space). Legacy TEI/local
    # bge-m3 below only activates when embedding_base_url is empty.
    dashscope_api_key: SecretStr = SecretStr("")

    # --- TEI (Text Embeddings Inference) — bge-m3 local embedding service ---
    tei_url: str = "http://localhost:8080"

    # --- bge-m3 local model (fallback when TEI is unreachable) ---
    # TEI 不可用时走本地 sentence-transformers。二选一(都不填则尝试 hub 下载,
    # 会被 SSL 拦):
    #   bge_m3_local_path: 直接指向模型目录(含 snapshot hash,机器特定)
    #   hf_hub_cache: HF cache 根,让 ``SentenceTransformer("BAAI/bge-m3")`` 按
    #                 refs/main 解析(推荐,不写死 hash,换机器只改根路径)
    bge_m3_local_path: str = ""
    hf_hub_cache: str = ""

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
    demo_db_ro_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/taskflow"

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
    # 预算硬上限（MAX_MODEL_CALLS / MAX_TOKENS_BUDGET / MAX_TIME_SECONDS）的单一来源
    # 是 engine/budget/constants.py，此处不再另存副本（§6.1 split-brain 根治）。
    # model_context_window / reserved_for_output / warning/critical ratio 等非上限
    # 参数由 ContextBudget 自带默认值，不在此暴露。

    # --- Tool Result Truncation ---
    # 当为 False 时，禁用所有工具结果的截断/压缩（用于调试诊断效果）。
    # 影响两处：
    #   1. engine.context.truncation.truncate_tool_result —— 入 context 前的字符上限
    #   2. observability_unified.search_observability —— 8000 字符 JSON 截断
    tool_result_truncation_enabled: bool = True

    # --- RAG Injection (episodic memory retrieval) ---
    # When False, the diagnosis agent skips historical-case retrieval entirely
    # (no Qdrant query, no injection). Demo/interactive keeps True; headless/CI
    # set RAG_INJECTION_ENABLED=false for reproducibility + speed (an empty
    # library is already neutral, but this avoids the embed/Qdrant round-trip).
    # Also the toggle for #2 ablation (RAG on vs off on the same case set).
    rag_injection_enabled: bool = True

    # --- P1-a root-cause recall tool (design §6.4) ---
    # Independent switch for the ``search_historical_root_cause`` agent tool,
    # which queries the ``root_cause`` named vector once the agent has formed a
    # root-cause hypothesis (breaks the P0 symptom-similarity ceiling, #8).
    # ``rag_injection_enabled`` gates the P0 *symptom* static injection (node-
    # side, pre-agent); this gates the *root-cause* tool (agent-side, on-demand)
    # -- two independent mechanisms, two independent switches. When False the
    # tool is still registered (stable schema) but returns a graceful "未启用"
    # string without hitting Qdrant (RAG is a gain, not a dependency).
    rag_root_cause_tool_enabled: bool = True

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
