#!/bin/bash
set -e

echo "Starting FastAPI on port 8000..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
FASTAPI_PID=$!

sleep 5

echo "Starting douyinLive service on port 1088..."
/usr/local/bin/douyinLive 2>&1 &

wait -n
