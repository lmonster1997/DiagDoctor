# ============================================================
# Stage 1: Install Python dependencies with uv
# ============================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# ── Debian mirror (Docker bypasses host VPN) ──
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources

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

# Install dependencies (use VPN for PyPI access)
ENV UV_HTTP_TIMEOUT=300
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
COPY scripts/ scripts/
COPY docker-entrypoint.sh .

# Make entrypoint executable
RUN chmod +x docker-entrypoint.sh

# Make sure the virtual env's site-packages are on PATH
ENV PATH="/app/.venv/bin:$PATH"

# OpenTelemetry
ENV OTEL_SERVICE_NAME=demo-backend

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Entrypoint: auto-migrate DB then start app
ENTRYPOINT ["/app/docker-entrypoint.sh"]
