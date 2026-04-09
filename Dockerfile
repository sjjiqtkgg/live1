# 使用官方 Python 3.12 镜像作为基础
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 1. 安装 Node.js（供 PyExecJS 执行 sign.js 使用）
RUN apt-get update && \
    apt-get install -y nodejs npm && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 2. 复制并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. 复制项目所有文件到镜像
COPY . .

# 4. 暴露端口（Render 会通过 $PORT 注入）
EXPOSE 10000

# 5. 启动命令
CMD uvicorn main:app --host 0.0.0.0 --port $PORT