FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 douyinLive 二进制（从 GitHub latest release）
RUN curl -sSL -H "Accept: application/octet-stream" \
    "https://github.com/jwwsjlm/douyinLive/releases/latest/download/douyinLive-linux-amd64" \
    -o /usr/local/bin/douyinLive && \
    chmod +x /usr/local/bin/douyinLive

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
