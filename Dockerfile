FROM ghcr.io/jwwsjlm/douyinlive:latest AS douyinlive

FROM python:3.11-slim

ENV PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=douyinlive /app/douyinLive /usr/local/bin/douyinLive

COPY config.yaml /app/config.yaml
COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
EXPOSE 1088

CMD ["/entrypoint.sh"]
