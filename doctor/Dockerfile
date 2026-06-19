# ============================================================
# Stage 1: Install Python dependencies with uv
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv via pip
RUN pip install uv --no-cache-dir

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# ---- Dependency layer (cache-friendly: copy only dep files first) ----
COPY pyproject.toml uv.lock* ./

# Install dependencies
ENV UV_HTTP_TIMEOUT=300
RUN uv sync --frozen --no-dev --no-install-project 2>/dev/null || uv sync --no-dev --no-install-project

# ============================================================
# Stage 2: Runtime (slim, no build tools)
# ============================================================
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create a non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy the virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy application source code
COPY src/ src/
COPY tests/ tests/
COPY pyproject.toml ./

# Set environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
