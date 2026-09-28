#!/bin/bash
set -e

# 优先用平台注入的 PORT，没有才回退 8000（兼容 Back4App 这类需要手动指定的平台）
APP_PORT="${PORT:-8000}"

echo "Starting FastAPI on port ${APP_PORT}..."
uvicorn main:app --host 0.0.0.0 --port "${APP_PORT}" &
FASTAPI_PID=$!

sleep 5

echo "Starting douyinLive service on port 1088..."
/usr/local/bin/douyinLive 2>&1 &

wait -n
