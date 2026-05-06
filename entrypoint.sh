#!/bin/bash
set -e

echo "Starting douyinLive service..."
# 后台启动 Go 弹幕服务，监听 1088 端口
/usr/local/bin/douyinLive &

# 等待服务完全启动（可适当延长）
sleep 5

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
