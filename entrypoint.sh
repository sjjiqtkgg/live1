#!/bin/bash
set -e

echo "Starting douyinLive service..."
# 使用 `exec` 替换当前 shell 进程，并使用 `&` 放入后台。
# 关键修改：增加 `2>&1 | while...` 将 Go 服务的输出也打印到主日志，方便调试。
/usr/local/bin/douyinLive 2>&1 | while IFS= read -r line; do
  echo "[douyinLive] $line"
done &

echo "[douyinLive] 服务已后台启动，等待5秒..."
sleep 5

echo "Starting FastAPI..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
