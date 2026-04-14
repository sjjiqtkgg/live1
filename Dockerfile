FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（Node.js 用于执行 sign.js）
RUN apt-get update && \
    apt-get install -y nodejs npm && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装 Python 包
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目所有文件
COPY . .

EXPOSE 10000

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
