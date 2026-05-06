#!/bin/bash
set -e

echo "Starting douyinLive service with config..."
/usr/local/bin/douyinLive --config /app/config.yaml 2>&1 &

sleep 5

echo "Starting FastAPI on port ${PORT:-8000}..."
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
