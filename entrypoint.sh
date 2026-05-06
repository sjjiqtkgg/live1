#!/bin/bash
set -e

echo "Starting douyinLive container..."
# 在 Render 容器内运行官方镜像，并映射 1088 端口
docker run -d --name douyinlive \
    --restart unless-stopped \
    -p 1088:1088 \
    ghcr.io/jwwsjlm/douyinlive:latest

sleep 3  # 等待服务启动

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
