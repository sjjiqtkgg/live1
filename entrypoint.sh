#!/bin/bash
set -e

echo "Starting douyinLive service..."
# 后台启动 Go 弹幕服务，并将日志输出到 stderr
/usr/local/bin/douyinLive 2>&1 &

# 等待服务完全启动
sleep 5

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
