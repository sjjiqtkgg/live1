#!/bin/bash
set -e

echo "Starting douyinLive..."
douyinLive &

sleep 2

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
