# 第一阶段：从官方镜像提取 douyinLive 二进制文件
FROM ghcr.io/jwwsjlm/douyinlive:latest AS douyinlive

# 第二阶段：构建 Python 服务
FROM python:3.11-slim AS final

# 安装基础工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 从官方镜像中复制 douyinLive 二进制文件
COPY --from=douyinlive /douyinLive /usr/local/bin/douyinLive

# 复制应用代码和启动脚本
COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
