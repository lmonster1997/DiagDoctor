# ============================================================
# Stage 1: Install Python dependencies with uv
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
# Link mode: copy (no hardlinks in Docker)
ENV UV_LINK_MODE=copy

# ---- Dependency layer (cache-friendly: copy only dep files first) ----
COPY pyproject.toml uv.lock* ./

# Install dependencies into the virtual environment
# --no-dev: skip dev dependencies in the production image
RUN uv sync --frozen --no-dev --no-install-project

# ============================================================
# Stage 2: Runtime (slim, no build tools)
# ============================================================
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create a non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy the virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy application source code (after deps for layer caching)
COPY alembic.ini .
COPY alembic/ alembic/
COPY app/ app/

# Make sure the virtual env's site-packages are on PATH
ENV PATH="/app/.venv/bin:$PATH"

# OpenTelemetry
ENV OTEL_SERVICE_NAME=demo-backend

# Expose port
EXPOSE 8000

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Startup
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
