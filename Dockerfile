FROM python:3.12-slim

WORKDIR /app

# 安装 Node.js
RUN apt-get update && \
    apt-get install -y nodejs npm && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
