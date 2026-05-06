FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl file \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 下载 douyinLive 二进制，并增加验证步骤
RUN curl -sSL -o /tmp/douyinLive "https://github.com/jwwsjlm/douyinLive/releases/latest/download/douyinLive-linux-amd64" && \
    # 检查文件类型是否为可执行文件
    file /tmp/douyinLive | grep -q "ELF" && \
    # 如果验证通过，再移动到正式目录并赋予执行权限
    mv /tmp/douyinLive /usr/local/bin/douyinLive && \
    chmod +x /usr/local/bin/douyinLive || \
    # 如果验证失败，清除文件并报错
    (rm -f /tmp/douyinLive && echo "错误：下载的文件不是有效的可执行程序" && exit 1)

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
