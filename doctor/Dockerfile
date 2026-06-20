# ============================================================
# Stage 1: Install Python dependencies with uv
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# ── Debian mirror (Docker bypasses host VPN) ──
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# Install system deps needed for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    -o Acquire::Retries=3 \
    && rm -rf /var/lib/apt/lists/*

# Install uv via pip
RUN pip install uv --no-cache-dir

# Enable bytecode compilation for faster startup
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# ---- Dependency layer (cache-friendly: copy only dep files first) ----
COPY pyproject.toml ./

# Install dependencies
ENV UV_HTTP_TIMEOUT=300
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN uv sync --no-dev --no-install-project

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
COPY pyproject.toml ./
COPY src/ src/

# Make sure the virtual env's site-packages are on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Python settings
ENV PYTHONUNBUFFERED=1

# OpenTelemetry
ENV OTEL_SERVICE_NAME=doctor-api

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run as non-root user
USER appuser

# Start the application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
