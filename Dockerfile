# ---- Stage 1: Build douyinLive ----
FROM golang:1.22-alpine AS builder
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth=1 https://github.com/jwwsjlm/douyinLive.git .
RUN CGO_ENABLED=0 go build -o /douyinLive ./cmd/main.go

# ---- Stage 2: Final image ----
FROM python:3.11-slim

# 安装系统依赖（如果 httpx 等需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制 Python 依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制 Go 二进制
COPY --from=builder /douyinLive /usr/local/bin/douyinLive

# 复制应用代码（包括 main.py, index.html, static 等）
COPY . .

# 复制启动脚本
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 声明运行时端口（FastAPI 默认 8000）
EXPOSE 8000

# 启动脚本负责运行两个进程
CMD ["/entrypoint.sh"]
