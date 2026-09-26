#!/bin/bash
set -e

# 1. 先启动 FastAPI，让它牢牢占住 8000 端口
echo "Starting FastAPI on port 8000..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
FASTAPI_PID=$!

# 2. 等待 FastAPI 完成监听，让 Back4App 探测到 8000
sleep 5

# 3. 再启动 Go 弹幕服务（跑在 1088 上，供 FastAPI 内部连接）
echo "Starting douyinLive service on port 1088..."
/usr/local/bin/douyinLive 2>&1 &

# 4. 维持容器运行（任一进程退出则容器退出）
wait -n
