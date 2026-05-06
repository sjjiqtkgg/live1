#!/bin/bash
set -e

echo "Starting douyinLive service..."
# 后台启动 Go 服务，监听 1088 端口
douyinLive &

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
