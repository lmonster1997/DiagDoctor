#!/bin/sh
set -e

echo "🔧 Running database migrations..."
/app/.venv/bin/alembic upgrade head

echo "🚀 Starting application (with log shipping to Loki)..."
# Pipe stdout+stderr through a Python script that ships each line to Loki
exec /app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 2>&1 | \
  /app/.venv/bin/python -c "
import sys, requests, time, json

LOKI_URL = 'http://loki:3100/loki/api/v1/push'
SERVICE_NAME = 'demo-backend'
batch = []
last_flush = time.monotonic()

for line in sys.stdin:
    line = line.rstrip('\n')
    if not line.strip():
        continue
    ts_ns = str(int(time.time() * 1_000_000_000))
    batch.append({
        'stream': {'service_name': SERVICE_NAME},
        'values': [[ts_ns, line]],
    })
    if len(batch) >= 20 or (time.monotonic() - last_flush) > 5:
        try:
            requests.post(LOKI_URL, json={'streams': batch}, timeout=5)
        except Exception:
            pass
        batch.clear()
        last_flush = time.monotonic()
"
