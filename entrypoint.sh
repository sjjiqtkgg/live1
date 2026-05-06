#!/bin/bash
set -e

echo "Starting douyinLive service..."
# 后台启动 Go 弹幕服务，并增加守护循环，防止进程意外退出
(
    while true; do
        /usr/local/bin/douyinLive
        echo "[守护] douyinLive 进程退出，5秒后自动重启..."
        sleep 5
    done
) &

# 等待服务完全启动（可适当延长）
sleep 5

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
