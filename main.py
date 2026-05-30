import json
import re
import os
import httpx
import asyncio
import websockets
import threading
import time
import hashlib
import base64
import random
import ssl
import traceback
from fastapi import FastAPI, Query, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse
from urllib.parse import unquote, urlparse, parse_qs, quote, urljoin

try:
    from streamget.platforms.soop.live_stream import SoopLiveStream
except ImportError:
    SoopLiveStream = None

try:
    from python_socks.sync import Proxy
    SOCKS_SUPPORT = True
except ImportError:
    SOCKS_SUPPORT = False
    print("[警告] python_socks 未安装，WebSocket 将不使用代理")

try:
    import websocket as websocket_client  # websocket-client 库，用于 Twitch IRC
    WEBSOCKET_CLIENT_AVAILABLE = True
except ImportError:
    WEBSOCKET_CLIENT_AVAILABLE = False
    print("[警告] websocket-client 未安装，Twitch 弹幕不可用")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    async def _cache_cleanup():
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [k for k, v in list(M3U8_CACHE.items()) if v.get('expire', 0) < now]
            for k in expired:
                M3U8_CACHE.pop(k, None)
            if expired:
                print(f"[缓存] 清理 {len(expired)} 条过期条目，剩余 {len(M3U8_CACHE)} 条")
            if len(STREAM_PROXY_MAP) > 500:
                keys = list(STREAM_PROXY_MAP.keys())
                for k in keys[:250]:
                    STREAM_PROXY_MAP.pop(k, None)
                print(f"[缓存] STREAM_PROXY_MAP 超限，已清理至 {len(STREAM_PROXY_MAP)} 条")
    task = asyncio.create_task(_cache_cleanup())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36"

PROXY_LIST_STR = os.getenv("PROXY_LIST", "")
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()] if PROXY_LIST_STR else [None]
print(f"[代理] 国内代理 {len(PROXY_URLS)} 个: {PROXY_URLS}")

EXTERNAL_PROXY_LIST_STR = os.getenv("EXTERNAL_PROXY_LIST", "")
EXTERNAL_PROXY_URLS = [p.strip() for p in EXTERNAL_PROXY_LIST_STR.split(",") if p.strip()] if EXTERNAL_PROXY_LIST_STR else [None]
print(f"[代理] 外网代理 {len(EXTERNAL_PROXY_URLS)} 个: {EXTERNAL_PROXY_URLS}")

CF_WORKER = os.getenv("CF_WORKER_URL", "")
SOOP_COOKIE = os.getenv("SOOP_COOKIE", "")
TWITCH_COOKIE = os.getenv("TWITCH_COOKIE", "")
if CF_WORKER:
    print(f"[CF Worker] 已配置: {CF_WORKER}")
else:
    print("[CF Worker] 未配置，海外平台 m3u8 将走外网代理")

CLIENT_POOL: dict = {}
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15

async def get_client(proxy=None, timeout=None):
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    async with CLIENT_LOCK:
        if key not in CLIENT_POOL:
            mounts = None
            proxy_arg = proxy
            
            # 处理 HTTPS 代理的自签名证书
            if isinstance(proxy, str) and proxy.startswith("https://"):
                transport = httpx.AsyncHTTPTransport(
                    proxy=proxy,
                    verify=False,  # 跳过代理证书验证
                    http2=True,
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
                )
                mounts = {"http://": transport, "https://": transport}
                proxy_arg = None  # 顶层不再设置 proxy
                
            CLIENT_POOL[key] = httpx.AsyncClient(
                timeout=timeout,
                proxy=proxy_arg,
                mounts=mounts,
                http2=True,
                verify=False,  # 跳过目标网站证书验证
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
            )
        return CLIENT_POOL[key]

STREAM_PROXY_MAP: dict = {}
M3U8_CACHE: dict = {}

def get_fixed_proxy_list(proxy_pool):
    if not proxy_pool or proxy_pool[0] is None:
        return [None]
    primary = random.choice(proxy_pool)
    rest = [p for p in proxy_pool if p != primary]
    return [primary] + rest

async def request_with_retry(method, url, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for idx, proxy in enumerate(PROXY_URLS):
        try:
            print(f"[请求重试] 尝试代理 [{idx+1}/{len(PROXY_URLS)}]: {proxy or '直连'}")
            client = await get_client(proxy, timeout)
            resp = await client.request(method, url, **kwargs)
            return resp
        except Exception as e:
            last_error = e
            print(f"[请求重试] 失败: {e}")
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    shuffle_proxy = kwargs.pop("shuffle_proxy", False)
    targets = proxy_list[:]
    if shuffle_proxy and targets:
        random.shuffle(targets)
    for idx, proxy in enumerate(targets):
        try:
            print(f"[分组请求] 使用代理 [{idx+1}/{len(targets)}]: {proxy or '直连'}")
            client = await get_client(proxy, timeout)
            resp = await client.request(method, url, **kwargs)
            if shuffle_proxy:
                try:
                    ip_resp = await client.get("https://api.ipify.org")
                    print(f"[出口IP] {ip_resp.text}")
                except Exception:
                    pass
            return resp
        except Exception as e:
            last_error = e
            print(f"[分组请求] 失败: {e}")
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

# 代理接口、平台解析、弹幕等剩余代码保持不变，此处省略以节省篇幅。
# 实际部署时请将之前完整的 main.py 中 get_client 替换为本版本，并保留后续所有函数。
# 后续代码包括虎牙、斗鱼、B站、抖音、Twitch、SOOP、PandaTV 解析函数，以及弹幕 WebSocket 等。
