# ---- Stage 1: 从官方镜像提取 douyinLive 二进制 ----
FROM ghcr.io/jwwsjlm/douyinlive:v2.0.26-beta.1 AS douyinlive

# ---- Stage 2: 构建我们的 Python 服务 ----
FROM python:3.11-slim

# 安装系统工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 从官方镜像中复制 douyinLive 二进制
COPY --from=douyinlive /app/douyinLive /usr/local/bin/douyinLive

# 复制应用代码和启动脚本
COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 【关键新增】强制 Render 只识别 8000 端口
ENV PORT=8000
EXPOSE 8000

CMD ["/entrypoint.sh"]
