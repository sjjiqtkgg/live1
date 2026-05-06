FROM python:3.11-slim

# 安装系统依赖（运行 douyinLive 和 Python 都需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制 Python 依赖并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 复制启动脚本
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 声明运行时端口（FastAPI 默认 8000）
EXPOSE 8000

# 启动脚本负责运行所有进程
CMD ["/entrypoint.sh"]
