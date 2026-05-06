FROM python:3.11-slim

# 安装基础工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 douyinLive 二进制 (固定使用 v0.3.12 版本，实测可用)
# 若需要更新版本，修改此标签即可
ENV DOUYINLIVE_VERSION=v0.3.12
RUN curl -sSL "https://github.com/jwwsjlm/douyinLive/releases/download/${DOUYINLIVE_VERSION}/douyinLive-linux-amd64" \
    -o /usr/local/bin/douyinLive && \
    chmod +x /usr/local/bin/douyinLive

# 复制应用代码和启动脚本
COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
