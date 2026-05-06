#!/bin/bash
set -e

echo "Starting douyinLive service on port 1088..."
/usr/local/bin/douyinLive 2>&1 &
sleep 5

echo "Starting FastAPI on port ${PORT:-8000}..."
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
