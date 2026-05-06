FROM ghcr.io/jwwsjlm/douyinlive:latest AS douyinlive

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=douyinlive /app/douyinLive /usr/local/bin/douyinLive

# 复制配置文件（放在应用代码之前或之后都行）
COPY config.yaml /app/config.yaml

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PORT=8000
EXPOSE 8000

CMD ["/entrypoint.sh"]
