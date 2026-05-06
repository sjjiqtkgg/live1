# ---- Stage 1: 从官方镜像提取 douyinLive 二进制 ----
FROM ghcr.io/jwwsjlm/douyinlive:latest AS douyinlive

# ---- Stage 2: 构建我们的 Python 服务 ----
FROM python:3.11-slim

# 安装系统工具（仅保留 ca-certificates 和 curl，curl 可能不需要，但保留无害）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 从官方镜像中复制 douyinLive 二进制
COPY --from=douyinlive /douyinLive /usr/local/bin/douyinLive

# 复制应用代码
COPY . .

# 复制启动脚本
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
