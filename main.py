import json
import re
import os
import httpx
import asyncio
import itertools
import websockets
import time
import hashlib
import base64
import random
import ssl
import traceback
import logging
from fastapi import FastAPI, Query, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, Response, JSONResponse
from urllib.parse import unquote, urlparse, parse_qs, quote, urljoin

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    SLOWAPI_AVAILABLE = True
except ImportError:
    SLOWAPI_AVAILABLE = False

# -------------------- 日志配置 --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

try:
    from streamget.platforms.soop.live_stream import SoopLiveStream
except ImportError:
    SoopLiveStream = None

try:
    from streamget import DouyinLiveStream
except ImportError:
    DouyinLiveStream = None
    logging.warning("streamget 未安装，抖音平台不可用")

try:
    from python_socks.sync import Proxy
    SOCKS_SUPPORT = True
except ImportError:
    SOCKS_SUPPORT = False
    logging.warning("python_socks 未安装，WebSocket 将不使用代理")

from contextlib import asynccontextmanager

# ==================== Go 弹幕服务配置 ====================
GO_DANMAKU_HOST = os.getenv("GO_DANMAKU_HOST", "localhost")
GO_DANMAKU_PORT = os.getenv("GO_DANMAKU_PORT", "1088")
GO_DANMAKU_BASE = f"ws://{GO_DANMAKU_HOST}:{GO_DANMAKU_PORT}"

_GO_SERVICE_AVAILABLE = False
_GO_SERVICE_LAST_CHECK = 0.0
_GO_SERVICE_CHECK_INTERVAL = 30.0
_GO_SERVICE_CHECK_LOCK = asyncio.Lock()

async def _check_go_service() -> bool:
    """TCP 探测 Go 弹幕服务是否在线，结果写入全局标志。"""
    global _GO_SERVICE_AVAILABLE, _GO_SERVICE_LAST_CHECK
    now = time.time()
    if now - _GO_SERVICE_LAST_CHECK < _GO_SERVICE_CHECK_INTERVAL:
        return _GO_SERVICE_AVAILABLE
    async with _GO_SERVICE_CHECK_LOCK:
        now = time.time()
        if now - _GO_SERVICE_LAST_CHECK < _GO_SERVICE_CHECK_INTERVAL:
            return _GO_SERVICE_AVAILABLE
        _GO_SERVICE_LAST_CHECK = now
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(GO_DANMAKU_HOST, int(GO_DANMAKU_PORT)),
                timeout=2.0
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if not _GO_SERVICE_AVAILABLE:
                logging.info(f"[Go弹幕] 服务已恢复: {GO_DANMAKU_BASE}")
            _GO_SERVICE_AVAILABLE = True
        except Exception:
            if _GO_SERVICE_AVAILABLE:
                logging.warning(f"[Go弹幕] 服务不可达: {GO_DANMAKU_BASE}，将停止接受抖音弹幕连接")
            _GO_SERVICE_AVAILABLE = False
        return _GO_SERVICE_AVAILABLE

@asynccontextmanager
async def lifespan(app):
    await _check_go_service()

    last_pool_rebuild = time.time()
    last_health_reset = time.time()

    async def _cache_cleanup():
        nonlocal last_pool_rebuild, last_health_reset
        while True:
            await asyncio.sleep(60)
            now = time.time()

            expired = [k for k, v in list(M3U8_CACHE.items()) if v.get('expire', 0) < now]
            for k in expired:
                M3U8_CACHE.pop(k, None)
            if expired:
                logging.info(f"[缓存] 清理 {len(expired)} 条过期条目，剩余 {len(M3U8_CACHE)} 条")

            if len(STREAM_PROXY_MAP) > 500:
                now_ts = time.time()
                safe_to_remove = [
                    k for k in list(STREAM_PROXY_MAP.keys())
                    if now_ts - STREAM_PROXY_MAP_TS.get(k, 0) > 60
                ]
                for k in safe_to_remove[:250]:
                    STREAM_PROXY_MAP.pop(k, None)
                    STREAM_PROXY_MAP_TS.pop(k, None)
                logging.info(f"[缓存] STREAM_PROXY_MAP 超限，已安全清理 {len(safe_to_remove[:250])} 条，剩余 {len(STREAM_PROXY_MAP)} 条")

            if len(_SOOP_OFFLINE_COUNT) > 1000:
                keys = list(_SOOP_OFFLINE_COUNT.keys())
                for k in keys[:500]:
                    _SOOP_OFFLINE_COUNT.pop(k, None)

            if len(_DOUYIN_NO_STREAM_LOGGED) > 1000:
                keys = list(_DOUYIN_NO_STREAM_LOGGED.keys())
                for k in keys[:500]:
                    _DOUYIN_NO_STREAM_LOGGED.pop(k, None)

            if now - last_pool_rebuild >= 3600:
                last_pool_rebuild = now
                async with CLIENT_LOCK:
                    idle_keys = [
                        k for k, (client, last_use) in list(CLIENT_POOL.items())
                        if now - last_use > 900
                    ]
                    for k in idle_keys:
                        entry = CLIENT_POOL.pop(k, None)
                        if entry:
                            client, _ = entry
                            try:
                                await client.aclose()
                            except Exception:
                                pass
                if idle_keys:
                    logging.info(f"[连接池] 惰性清理 {len(idle_keys)} 个空闲连接，剩余 {len(CLIENT_POOL)} 个")

            if now - last_health_reset >= 6 * 3600:
                last_health_reset = now
                if _PROXY_HEALTH:
                    logging.info(f"[代理健康] 周期性重置统计，重置前共 {len(_PROXY_HEALTH)} 个代理记录")
                    _PROXY_HEALTH.clear()

            if not _GO_SERVICE_AVAILABLE:
                await _check_go_service()

    task = asyncio.create_task(_cache_cleanup())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    async with CLIENT_LOCK:
        for k, (client, _) in list(CLIENT_POOL.items()):
            try:
                await client.aclose()
            except Exception:
                pass
        CLIENT_POOL.clear()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

# ==================== 安全配置 ====================
ALLOWED_HOSTS = [h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
ALLOWED_WS_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_WS_ORIGINS", "").split(",") if o.strip()]
MAX_TS_SIZE = int(os.getenv("MAX_TS_SIZE", str(20 * 1024 * 1024)))

if ALLOWED_HOSTS:
    logging.info(f"[安全] Host 白名单已启用: {ALLOWED_HOSTS}")
if ALLOWED_WS_ORIGINS:
    logging.info(f"[安全] WebSocket Origin 白名单已启用: {ALLOWED_WS_ORIGINS}")
logging.info(f"[安全] TS 切片大小上限: {MAX_TS_SIZE} 字节")

@app.middleware("http")
async def host_header_guard(request: Request, call_next):
    if ALLOWED_HOSTS:
        raw_host = request.headers.get("host", "")
        host = raw_host.split(":")[0].strip().lower()
        if host and host not in ALLOWED_HOSTS:
            logging.warning(f"[安全] 拒绝非法 Host 头: {raw_host!r}")
            return JSONResponse({"error": "invalid host header"}, status_code=400)
    return await call_next(request)

def get_real_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

if SLOWAPI_AVAILABLE:
    limiter = Limiter(key_func=get_real_ip)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
else:
    logging.warning("[限流] slowapi 未安装，/api/parse 与 /api/proxy 不受限流保护")
    class _NoopLimiter:
        def limit(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator
    limiter = _NoopLimiter()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36"

# ==================== 代理配置 ====================
PROXY_LIST_STR = os.getenv("PROXY_LIST", "")
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()] if PROXY_LIST_STR else [None]
logging.info(f"[代理] 国内代理 {len(PROXY_URLS)} 个: {PROXY_URLS}")

EXTERNAL_PROXY_LIST_STR = os.getenv("EXTERNAL_PROXY_LIST", "")
EXTERNAL_PROXY_URLS = [p.strip() for p in EXTERNAL_PROXY_LIST_STR.split(",") if p.strip()] if EXTERNAL_PROXY_LIST_STR else [None]
logging.info(f"[代理] 外网代理 {len(EXTERNAL_PROXY_URLS)} 个: {EXTERNAL_PROXY_URLS}")

CF_WORKER = os.getenv("CF_WORKER_URL", "").strip()
SOOP_COOKIE = os.getenv("SOOP_COOKIE", "").strip()
TWITCH_COOKIE = os.getenv("TWITCH_COOKIE", "").strip()
BILI_COOKIE = os.getenv("BILI_COOKIE", "").strip()

if CF_WORKER:
    logging.info(f"[CF Worker] 已配置: {CF_WORKER}")
else:
    logging.info("[CF Worker] 未配置，海外平台 m3u8 将走外网代理")

# ==================== 全局连接池 ====================
CLIENT_POOL: dict = {}
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15
VERIFY_TLS = os.getenv("VERIFY_TLS", "0") == "1"

async def get_client(proxy=None, timeout=None):
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    entry = CLIENT_POOL.get(key)
    if entry is not None:
        client, _ = entry
        CLIENT_POOL[key] = (client, time.time())
        return client
    async with CLIENT_LOCK:
        entry = CLIENT_POOL.get(key)
        if entry is not None:
            client, _ = entry
            CLIENT_POOL[key] = (client, time.time())
            return client
        client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy,
            http2=True,
            verify=VERIFY_TLS,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20
            )
        )
        CLIENT_POOL[key] = (client, time.time())
        return client

STREAM_PROXY_MAP: dict = {}
STREAM_PROXY_MAP_TS: dict = {}
M3U8_CACHE: dict = {}
_SOOP_OFFLINE_COUNT: dict = {}
_DOUYIN_NO_STREAM_LOGGED: dict = {}

_PROXY_HEALTH: dict = {}

def _record_proxy_health(proxy, ok: bool, tag: str = None, error: str = None):
    key = proxy or "直连"
    h = _PROXY_HEALTH.setdefault(key, {
        "success": 0, "fail": 0,
        "last_success_ts": None, "last_fail_ts": None,
        "last_error": None, "by_tag": {}
    })
    if ok:
        h["success"] += 1
        h["last_success_ts"] = time.time()
    else:
        h["fail"] += 1
        h["last_fail_ts"] = time.time()
        h["last_error"] = error
    if tag:
        t = h["by_tag"].setdefault(tag, {"success": 0, "fail": 0})
        t["success" if ok else "fail"] += 1

_STREAM_TAGS = ("SOOP-live", "SOOP-cdn", "SOOP-master", "SOOP-aid",
                 "Twitch-m3u8", "Twitch-token", "PandaTV-play", "PandaTV-master")

def _short_url(url: str, length: int = 60) -> str:
    path = url.split('?')[0]
    return path if len(path) <= length else path[:length] + "…"

_PROXY_RR_LOCK = asyncio.Lock()
_PROXY_RR_CYCLES: dict = {}

def _get_rr_cycle(proxy_pool):
    pool_id = id(proxy_pool)
    if pool_id not in _PROXY_RR_CYCLES:
        _PROXY_RR_CYCLES[pool_id] = itertools.cycle(range(len(proxy_pool)))
    return _PROXY_RR_CYCLES[pool_id]

async def get_fixed_proxy_list(proxy_pool):
    if not proxy_pool:
        return [None]
    if len(proxy_pool) == 1 and proxy_pool[0] is None:
        return [None]
    n = len(proxy_pool)
    async with _PROXY_RR_LOCK:
        cycle = _get_rr_cycle(proxy_pool)
        start = next(cycle)
    return [proxy_pool[(start + i) % n] for i in range(n)]

async def _sequential_request(method, url, proxy_list, fail_log_prefix, timeout, log_tag, **kwargs):
    last_error = None
    last_index = len(proxy_list) - 1
    for i, proxy in enumerate(proxy_list):
        try:
            client = await get_client(proxy, timeout)
            resp = await client.request(method, url, **kwargs)
            _record_proxy_health(proxy, True, tag=log_tag)
            if log_tag:
                logging.info(f"[{log_tag}] {proxy or '直连'} → HTTP {resp.status_code} {_short_url(url)}")
            return resp
        except Exception as e:
            _record_proxy_health(proxy, False, tag=log_tag, error=f"{type(e).__name__}: {e}")
            last_error = e
            logging.warning(f"{fail_log_prefix}{f'[{log_tag}]' if log_tag else ''} {proxy or '直连'} 失败 [{type(e).__name__}]: {e}")
            if i != last_index:
                await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

async def request_with_retry(method, url, **kwargs):
    timeout = kwargs.pop("timeout", 15)
    log_tag = kwargs.pop("log_tag", None)
    return await _sequential_request(method, url, PROXY_URLS, "[请求重试]", timeout, log_tag, **kwargs)

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    timeout = kwargs.pop("timeout", 15)
    shuffle_proxy = kwargs.pop("shuffle_proxy", False)
    log_tag = kwargs.pop("log_tag", None)

    targets = proxy_list[:]
    if shuffle_proxy and targets:
        random.shuffle(targets)

    return await _sequential_request(method, url, targets, "[分组请求]", timeout, log_tag, **kwargs)


async def stream_request_with_proxy_group(method, url, proxy_list, headers=None, content=None,
                                           shuffle_proxy=False, timeout=15, log_tag=None):
    proxies = proxy_list[:] if proxy_list else [None]
    if shuffle_proxy and proxies:
        random.shuffle(proxies)

    last_error = None
    last_index = len(proxies) - 1
    for i, proxy in enumerate(proxies):
        try:
            client = await get_client(proxy, timeout)
            req = client.build_request(method, url, headers=headers, content=content)
            resp = await client.send(req, stream=True)
            _record_proxy_health(proxy, True, tag=log_tag)
            if log_tag:
                logging.info(f"[{log_tag}] {proxy or '直连'} → HTTP {resp.status_code} {_short_url(url)}（流式）")
            return resp
        except Exception as e:
            _record_proxy_health(proxy, False, tag=log_tag, error=f"{type(e).__name__}: {e}")
            last_error = e
            logging.warning(f"[流式代理]{f'[{log_tag}]' if log_tag else ''} {proxy or '直连'} 失败 [{type(e).__name__}]: {e}")
            if i != last_index:
                await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败（流式）")


async def request_race(method, url, proxy_list, **kwargs):
    timeout = kwargs.pop("timeout", 15)
    log_tag = kwargs.pop("log_tag", None)
    kwargs.pop("shuffle_proxy", None)

    if not proxy_list:
        proxy_list = [None]

    if len(proxy_list) == 1:
        client = await get_client(proxy_list[0], timeout)
        resp = await client.request(method, url, **kwargs)
        _record_proxy_health(proxy_list[0], True, tag=log_tag)
        if log_tag:
            logging.info(f"[{log_tag}] {proxy_list[0] or '直连'} → HTTP {resp.status_code} {_short_url(url)}")
        return resp

    async def _try(proxy):
        client = await get_client(proxy, timeout)
        return proxy, await client.request(method, url, **kwargs)

    task_proxy: dict = {asyncio.create_task(_try(p)): p for p in proxy_list}
    pending: set = set(task_proxy)
    errors = []
    winner = None

    while pending and winner is None:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            proxy = task_proxy[task]
            try:
                _, resp = task.result()
                _record_proxy_health(proxy, True, tag=log_tag)
                if winner is None:
                    winner = (proxy, resp)
            except Exception as e:
                _record_proxy_health(proxy, False, tag=log_tag, error=f"{type(e).__name__}: {e}")
                errors.append(e)
                logging.warning(f"[竞速]{f'[{log_tag}]' if log_tag else ''} {proxy or '直连'} 失败 [{type(e).__name__}]: {e}")

    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if winner:
        proxy, resp = winner
        if log_tag:
            logging.info(f"[{log_tag}] {proxy or '直连'} 竞速胜出 → HTTP {resp.status_code} {_short_url(url)}")
        return resp

    raise errors[-1] if errors else Exception("所有代理均失败")

# ------------------ 代理接口 -----------------
@app.api_route("/api/proxy", methods=["GET", "POST"])
@limiter.limit("60/minute")
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = [
        ".douyu.com", ".huya.com", ".bilibili.com", ".bilivideo.com", ".douyucdn.cn",
        ".douyin.com", ".live.bilibili.com", ".twitch.tv", ".ttvnw.net",
        ".sooplive.com", ".sooplive.net", ".sooplivecdn.com",
        ".pandalive.co.kr",
        ".live-video.net",
        ".pandalivecdn.com",
    ]

    def is_allowed_domain(url: str) -> bool:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        for domain in ALLOWED:
            clean = domain.lstrip('.')
            if hostname == clean or hostname.endswith('.' + clean):
                return True
        return False

    if not is_allowed_domain(url):
        raise HTTPException(403, "domain not allowed")

    body = await request.body() if request.method == "POST" else None
    headers = {"User-Agent": ua or UA, "Referer": referer or ""}
    if cookie:
        headers["Cookie"] = cookie
    if request.method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    EXTERNAL_DOMAINS = [
        "twitch.tv", "ttvnw.net", "twitchsvc.net",
        "sooplive.com", "livestream-manager.sooplive.com",
        "pandalive.co.kr", "live-video.net", "pandalivecdn.com",
    ]
    EXTERNAL_REFERERS = ["twitch.tv", "player.twitch.tv", "sooplive.com", "pandalive.co.kr"]
    use_external = (
        any(d in url for d in EXTERNAL_DOMAINS) or
        any(d in (referer or "") for d in EXTERNAL_REFERERS)
    )
    proxy_list = EXTERNAL_PROXY_URLS if use_external else PROXY_URLS
    is_ts = url.lower().split("?")[0].endswith(".ts")

    if is_ts:
        stream_key = hashlib.md5(url.encode()).hexdigest()[:16]
        if stream_key not in STREAM_PROXY_MAP:
            if proxy_list and proxy_list[0] is not None:
                STREAM_PROXY_MAP[stream_key] = random.choice(proxy_list)
            else:
                STREAM_PROXY_MAP[stream_key] = None
            STREAM_PROXY_MAP_TS[stream_key] = time.time()
        else:
            STREAM_PROXY_MAP_TS[stream_key] = time.time()
        fixed_proxy = STREAM_PROXY_MAP[stream_key]
        proxies_to_use = [fixed_proxy] if fixed_proxy else proxy_list
        shuffle_proxy = False
    else:
        proxies_to_use = proxy_list
        shuffle_proxy = True

    resp = await stream_request_with_proxy_group(
        request.method, url,
        proxy_list=proxies_to_use,
        headers=headers, content=body,
        shuffle_proxy=shuffle_proxy,
    )
    content_type = resp.headers.get("content-type", "")
    is_m3u8 = "mpegurl" in content_type.lower() or url.split("?")[0].endswith(".m3u8")

    if is_m3u8:
        try:
            await resp.aread()
        finally:
            await resp.aclose()
        base_url = url.rsplit("/", 1)[0] + "/"
        parsed_cdn = urlparse(url)
        cdn_origin = f"{parsed_cdn.scheme}://{parsed_cdn.netloc}"
        proxy_base = str(request.base_url).rstrip("/") + "/api/proxy"
        _is_foreign_stream = any(d in url for d in ("live-video.net", "pandalive", "sooplive", "ttvnw", "twitch"))
        if CF_WORKER and _is_foreign_stream:
            proxy_base = CF_WORKER.rstrip("/")
        if referer:
            eff_referer = referer
        elif "pandalive" in url or "live-video.net" in url:
            eff_referer = "https://www.pandalive.co.kr/"
        elif "twitch" in url or "ttvnw" in url:
            eff_referer = "https://player.twitch.tv"
        else:
            eff_referer = "https://play.sooplive.com"

        text = resp.text
        lines = text.splitlines()
        rewritten = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#EXT-X-MEDIA:") or stripped.startswith("#EXT-X-MAP:") or stripped.startswith("#EXT-X-KEY:"):
                def replace_uri(match):
                    uri = match.group(1)
                    if uri.startswith("http://") or uri.startswith("https://"):
                        abs_uri = uri
                    elif uri.startswith("/"):
                        abs_uri = cdn_origin + uri
                    else:
                        abs_uri = base_url + uri
                    proxied = f"{proxy_base}?url={quote(abs_uri, safe='')}&referer={quote(eff_referer, safe='')}"
                    return f'URI="{proxied}"'
                new_line = re.sub(r'URI="([^"]+)"', replace_uri, line)
                rewritten.append(new_line)
                continue

            if stripped and not stripped.startswith("#"):
                if stripped.startswith("http://") or stripped.startswith("https://"):
                    abs_url = stripped
                elif stripped.startswith("/"):
                    abs_url = cdn_origin + stripped
                else:
                    abs_url = base_url + stripped
                proxied = f"{proxy_base}?url={quote(abs_url, safe='')}&referer={quote(eff_referer, safe='')}"
                rewritten.append(proxied)
            else:
                rewritten.append(line)

        body_out = "\n".join(rewritten).encode("utf-8")
        out_headers = {
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "application/vnd.apple.mpegurl",
        }
        return Response(content=body_out, status_code=resp.status_code, headers=out_headers)

    if is_ts:
        async def _stream_and_close():
            total = 0
            truncated = False
            try:
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_TS_SIZE:
                        truncated = True
                        logging.warning(f"[代理] TS 切片超过 {MAX_TS_SIZE} 字节，提前截断: {_short_url(url)}")
                        break
                    yield chunk
            finally:
                try:
                    await resp.aclose()
                except Exception:
                    pass
                if truncated:
                    logging.info(f"[代理] TS 截断完成，共转发 {total} 字节")

        return StreamingResponse(
            _stream_and_close(),
            status_code=resp.status_code,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": content_type or "video/mp2t"
            }
        )
    async def _stream_and_close_fallback():
        total = 0
        truncated = False
        try:
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_TS_SIZE:
                    truncated = True
                    logging.warning(f"[代理] 响应体超过 {MAX_TS_SIZE} 字节，提前截断: {_short_url(url)}")
                    break
                yield chunk
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            if truncated:
                logging.info(f"[代理] 兜底分支截断完成，共转发 {total} 字节")

    out_headers = {"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "application/json"}
    return StreamingResponse(_stream_and_close_fallback(), status_code=resp.status_code, headers=out_headers)


def build_streams(flv, m3u8):
    s = []
    if flv and flv.startswith("http"):
        s.append({"cdn": "FLV", "url": flv, "type": "flv"})
    if m3u8 and m3u8.startswith("http"):
        s.append({"cdn": "HLS", "url": m3u8, "type": "m3u8"})
    return s

def parse_multivariant_m3u8(text, base_url, cdn_prefix):
    streams = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        name = "Source"
        res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
        if res_match:
            name = res_match.group(1).split('x')[1] + 'p'
        else:
            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
            if bw_match:
                kbps = int(int(bw_match.group(1)) / 1000)
                name = f"{kbps}k"
        if i + 1 < len(lines):
            sub_url = lines[i + 1].strip()
            if not sub_url:
                continue
            if not sub_url.startswith("http"):
                sub_url = urljoin(base_url, sub_url)
            streams.append({"cdn": f"{cdn_prefix}-{name}", "url": sub_url, "type": "m3u8"})

    if streams:
        def _quality_key(s):
            n = s['cdn'].replace(f'{cdn_prefix}-', '')
            if 'Source' in n:
                return 999999
            num = re.search(r'\d+', n)
            return float(num.group()) if num else 0
        streams.sort(key=_quality_key, reverse=True)
    return streams

# ==================== 虎牙 ====================
def _extract_huya_danmaku_params(live):
    try:
        profile_info = live.get("profileInfo", {})
        live_data = live.get("liveData", {})

        uid = int(
            profile_info.get("lUid") or profile_info.get("uid") or
            live_data.get("lUid") or live_data.get("uid") or
            live_data.get("lYyid") or profile_info.get("lYyid") or 0
        )

        return {"platform": "huya", "uid": uid, "ayyuid": uid}
    except Exception:
        return {}


def huya_build_anticode(raw_anti, stream_name):
    anti = raw_anti.replace("&amp;", "&")
    params = dict(p.split("=", 1) for p in anti.split("&") if "=" in p)
    fm = params.get("fm", "")
    ws_time = params.get("wsTime", "")
    if not fm or not ws_time:
        return anti
    try:
        fm_dec = base64.b64decode(fm.replace("%2B", "+").replace("%2F", "/").replace("%3D", "=") + "==").decode()
    except Exception:
        try:
            fm_dec = base64.b64decode(unquote(fm) + "==").decode()
        except Exception:
            return anti
    p = fm_dec.split("_")[0]
    seqid = str(int(time.time() * 10000 + random.random() * 10000))
    ws_secret = hashlib.md5(f"{p}_0_{stream_name}_{seqid}_{ws_time}".encode()).hexdigest()
    params["wsSecret"] = ws_secret
    params["seqid"] = seqid
    params["u"] = "0"
    return "&".join(f"{k}={v}" for k, v in params.items())

async def parse_huya(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        CDN_NAMES = {"AL": "阿里云", "TX": "腾讯云", "HW": "华为云", "WS": "网宿", "BD": "百度云"}
        CDN_ORDER = {"TX": 0, "AL": 1, "HW": 2, "WS": 3, "BD": 4}
        resp = await request_with_retry("GET", f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}",
            headers={"User-Agent": UA, "Referer": "https://www.huya.com/"})
        data = resp.json()
        if data.get("status") != 200:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        live = data["data"]

        profile_info = live.get("profileInfo", {})
        live_data = live.get("liveData", {})

        anchor_name = (
            profile_info.get("nick") or live_data.get("nick") or
            profile_info.get("sNick") or live_data.get("sNick") or ""
        )
        if not anchor_name:
            try:
                mob_resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}",
                    headers={"User-Agent": MOBILE_UA, "Referer": "https://www.huya.com/"})
                mob_html = mob_resp.text
                m = re.search(r'"nick":"([^"]+)"', mob_html) or re.search(r'<title>([^_<]+)', mob_html)
                if m:
                    anchor_name = m.group(1).strip()
                else:
                    logging.warning(f"[虎牙] {room_id} 网页抓取未匹配到昵称")
            except Exception:
                logging.warning(f"[虎牙] {room_id} 网页抓取请求失败")
        anchor_name = anchor_name or "虎牙主播"

        avatar = (
            profile_info.get("avatar180") or profile_info.get("sAvatar180") or
            profile_info.get("sAvatar") or profile_info.get("avatar") or
            live_data.get("avatar180") or live_data.get("sAvatar180") or
            live_data.get("sAvatar") or live_data.get("avatar") or ""
        )

        if live.get("realLiveStatus") != "ON":
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        # 【虎牙画质字段名修复】兼容两套命名：虎牙 bitRate/name，斗鱼 iBitRate/sDisplayName
        bitrate_list = live_data.get("bitRateInfo") or live.get("bitRateInfo") or live.get("liveBitRateInfo") or []
        if isinstance(bitrate_list, str):
            try:
                bitrate_list = json.loads(bitrate_list)
            except Exception:
                bitrate_list = []
        qualities = []
        seen_bitrate = set()
        for item in bitrate_list or []:
            if not isinstance(item, dict):
                continue
            raw_b = item.get("bitRate")
            if raw_b is None:
                raw_b = item.get("iBitRate")
            try:
                b = int(raw_b or 0)
            except (TypeError, ValueError):
                b = 0
            name = (item.get("name") or item.get("sDisplayName") or "").strip()
            if not name or b in seen_bitrate:
                continue
            seen_bitrate.add(b)
            qualities.append((b, name))
        qualities.sort(key=lambda x: (0 if x[0] == 0 else 1, -x[0]))
        if not qualities:
            qualities = [(0, "原画"), (4000, "蓝光"), (2000, "超清"), (1000, "高清"), (500, "标清")]

        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        if not cdn_list:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        cdn_list.sort(key=lambda s: CDN_ORDER.get(s.get("sCdnType", "ZZ"), 9))

        streams = []
        seen_urls = set()

        for cdn in cdn_list:
            flv_url = cdn.get("sFlvUrl", "")
            base_stream_name = cdn.get("sStreamName", "")
            anti_code = cdn.get("sFlvAntiCode", "")
            suffix = cdn.get("sFlvUrlSuffix", "flv")
            line_index = cdn.get("iLineIndex", "")
            cdn_type = cdn.get("sCdnType", "")
            if line_index not in ("", None):
                line_label = f"线路{line_index}"
            else:
                line_label = CDN_NAMES.get(cdn_type, cdn_type or "线路")

            if not (flv_url and base_stream_name and anti_code):
                continue

            line_bitrates = set()
            for bitrate, quality_name in qualities:
                if bitrate in line_bitrates:
                    continue
                line_bitrates.add(bitrate)

                stream_name = base_stream_name if bitrate == 0 else f"{base_stream_name}_{bitrate}"
                built = huya_build_anticode(anti_code, stream_name)
                full_url = f"{flv_url}/{stream_name}.{suffix}?{built}"
                full_url = full_url.replace("http://", "https://")

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                streams.append({
                    "cdn": f"{line_label}-{quality_name}",
                    "line": line_label,
                    "quality": quality_name,
                    "url": full_url,
                    "type": "flv"
                })

        if not streams:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        danmaku = _extract_huya_danmaku_params(live)
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku, "isLive": True}
    except Exception as e:
        logging.exception("[虎牙] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 斗鱼 ====================
async def parse_douyu(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]

        # 【斗鱼画质修复】构造设备 ID，用作 Cookie 中的 dy_did / acf_did。
        # 斗鱼服务端通过这两个 cookie 判断请求是否来自"真实浏览器会话"。
        # 之前后端裸请求（无 Cookie）会被静默降级：rate=0 请求"原画2K60"，
        # 服务端下发的播放地址实际只有 _4000.flv（蓝光4M），导致最高档丢失。
        # 补上设备 cookie + Origin 头后，未登录状态也能拿到最高档。
        did = hashlib.md5(f"douyu_{room_id}_{int(time.time() // 3600)}_{random.randint(0, 9999)}".encode()).hexdigest()[:32].ljust(32, "0")
        device_cookie = f"dy_did={did}; acf_did={did}"
        hdrs = {
            "User-Agent": UA,
            "Referer": f"https://www.douyu.com/{room_id}",
            "Origin": "https://www.douyu.com",
            "Cookie": device_cookie,
        }

        info_resp = await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)
        info = info_resp.json()
        room = info.get("room")
        if not room:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}

        name = "斗鱼主播"
        avatar = ""

        try:
            open_api = f"https://open.douyucdn.cn/api/RoomApi/room/{room_id}"
            open_resp = await request_with_retry("GET", open_api, headers=hdrs)
            if open_resp.status_code == 200:
                open_data = open_resp.json()
                if open_data.get("error") == 0:
                    d = open_data.get("data", {})
                    name = d.get("owner_name") or name
                    avatar = d.get("avatar_big") or d.get("avatar_middle") or d.get("avatar") or ""
        except Exception:
            pass

        if not avatar or name == "斗鱼主播":
            name = room.get("nickname") or name
            owner = room.get("owner", {})
            avatar_obj = owner.get("avatar", {})
            if isinstance(avatar_obj, dict):
                avatar = avatar_obj.get("big") or avatar_obj.get("middle") or avatar_obj.get("small") or avatar
            else:
                avatar = room.get("avatar") or room.get("room_pic") or avatar

        if avatar and avatar.startswith("//"):
            avatar = "https:" + avatar

        if room.get("show_status") != 1 or room.get("videoLoop") == 1:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        real_id = str(room["room_id"])
        # 【斗鱼画质修复】did 已在函数开头构造，此处不再重复生成，保证 getEncryption 和
        # 后续 play 请求使用同一个设备标识（服务端可能校验 did 一致性）。
        enc_resp = await request_with_retry("GET", f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}",
                                            headers=hdrs)
        enc_data = enc_resp.json()
        if enc_data.get("error") != 0:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        white = enc_data["data"]
        ts = int(time.time())
        secret = white['rand_str']
        for _ in range(white['enc_time']):
            secret = hashlib.md5((secret + white['key']).encode()).hexdigest()
        suffix = f"{real_id}{ts}" if not white.get('is_special', False) else ""
        auth = hashlib.md5((secret + white['key'] + suffix).encode()).hexdigest()
        base_params = {
            'ver': '219032101',
            'iar': '0',
            'ive': '0',
            'rid': real_id,
            'hevc': '1',
            'fa': '0',
            'sov': '0',
            'enc_data': white['enc_data'],
            'tt': str(ts),
            'did': did,
            'auth': auth,
        }

        probe_params = dict(base_params, rate="0")
        try:
            await asyncio.sleep(random.uniform(0, 0.15))
            probe = await request_with_retry("POST",
                f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}",
                headers=hdrs, data=probe_params, timeout=10)
            probe = probe.json()
        except Exception as e:
            logging.warning(f"[斗鱼] 线路/画质表探测失败: {e}")
            probe = {}
        if probe.get("error") != 0 or not isinstance(probe.get("data"), dict):
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        pdata = probe["data"]
        logging.info(f"[斗鱼] room={room_id} cdnsWithName={pdata.get('cdnsWithName')!r}")
        logging.info(f"[斗鱼] room={room_id} multirates={pdata.get('multirates')!r}")

        lines, seen_cdn = [], set()
        for entry in pdata.get("cdnsWithName") or []:
            cdn_code = str(entry.get("cdn") or "").strip()
            if not cdn_code or cdn_code in seen_cdn:
                continue
            seen_cdn.add(cdn_code)
            lines.append({"name": (entry.get("name") or "").strip() or cdn_code, "cdn": cdn_code})
        if not lines:
            lines = [{"name": "主线路", "cdn": None}]

        rates, seen_rate = [], set()
        for entry in pdata.get("multirates") or []:
            try:
                rv = int(entry.get("rate"))
            except (TypeError, ValueError):
                continue
            if rv in seen_rate:
                continue
            seen_rate.add(rv)
            rates.append({"name": (entry.get("name") or "").strip() or f"画质{rv}",
                          "rate": rv, "bit": int(entry.get("bit") or 0)})
        if not rates:
            rates = [{"name": "原画", "rate": 0, "bit": 0}]
        name_by_bit = {r_["bit"]: r_["name"] for r_ in rates if r_["bit"]}

   async def fetch_combo(line_name, cdn_code, rate_info):
    params = base_params.copy()
    params["rate"] = str(rate_info["rate"])
    if cdn_code:
        params["cdn"] = cdn_code
    try:
        await asyncio.sleep(random.uniform(0, 0.15))
        r = await request_with_retry("POST",
            f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}",
            headers=hdrs, data=params, timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d.get("error") == 0:
                info = d["data"]
                url = f"{info['rtmp_url']}/{info['rtmp_live']}"
                # 【诊断】打印请求的 rate 和实际下发的 URL 后缀，可以直接看到是否被降级
                m_suffix = re.search(r"_([0-9]{3,6})\.flv", url)
                actual_suffix = m_suffix.group(1) if m_suffix else "无后缀(原画)"
                logging.info(f"[斗鱼] {line_name} 请求 rate={rate_info['rate']}({rate_info['name']}) → 实际后缀={actual_suffix}")
                return line_name, rate_info, url
            logging.warning(f"[斗鱼] {line_name} 画质 rate={rate_info['rate']} 接口返回错误: {d.get('error')} {d.get('msg','')}")
        else:
            logging.warning(f"[斗鱼] {line_name} 画质 rate={rate_info['rate']} HTTP {r.status_code}")
    except Exception as e:
        logging.warning(f"[斗鱼] {line_name} 画质 rate={rate_info['rate']} 请求异常: {e}")
    return line_name, rate_info, None

        combos = [(l_["name"], l_["cdn"], r_) for l_ in lines for r_ in rates]
        results = []
        for chunk in [combos[i:i + 4] for i in range(0, len(combos), 4)]:
            results += await asyncio.gather(*(fetch_combo(*cb) for cb in chunk))

        streams = []
        seen_urls = set()
        seen_pair = set()
        for line_name, rate_info, flv_url in results:
            if not flv_url or flv_url in seen_urls:
                continue
            seen_urls.add(flv_url)
            m = re.search(r"_([0-9]{3,6})\.flv", flv_url)
            actual_bit = int(m.group(1)) if m else rate_info["bit"]
            key = (line_name, actual_bit)
            if key in seen_pair:
                continue
            seen_pair.add(key)
            q_name = name_by_bit.get(actual_bit) or rate_info["name"]
            streams.append({
                "cdn": f"{line_name}-{q_name}",
                "line": line_name,
                "quality": q_name,
                "url": flv_url,
                "type": "flv"
            })

        if not streams:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        return {"streams": streams, "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        logging.exception("[斗鱼] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== B站 ====================
async def parse_bilibili(url):
    try:
        rid = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
        room_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers=hdrs)
        room_data = room_resp.json()
        if room_data.get("code") != 0:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        real_rid = room_data["data"]["room_id"]

        name, avatar = "B站主播", ""
        try:
            anchor_resp = await request_with_retry("GET",
                f"https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid={rid}",
                headers=hdrs)
            anchor_data = anchor_resp.json()
            if anchor_data.get("code") == 0:
                info = anchor_data.get("data", {}).get("info", {})
                name = info.get("uname") or name
                avatar = info.get("face") or ""
        except Exception:
            pass

        if room_data["data"].get("live_status") != 1:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        play_resp = await request_with_retry("GET",
            f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8",
            headers=hdrs)
        play = play_resp.json()
        if play.get("code") != 0:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        playurl = play["data"].get("playurl_info", {}).get("playurl", {})
        streams, seen = [], set()
        for stream in playurl.get("stream", []):
            for fmt in stream.get("format", []):
                for codec in fmt.get("codec", []):
                    for info in codec.get("url_info", []):
                        u = info["host"] + codec["base_url"] + info["extra"]
                        if u not in seen:
                            seen.add(u)
                            m = re.search(r"([a-z0-9]+)\.bilivideo", info["host"])
                            streams.append({"cdn": f"{fmt['format_name'].upper()}-{m.group(1) if m else 'cdn'}",
                                           "url": u, "type": "flv" if fmt["format_name"] == "flv" else "m3u8"})
        if not streams:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        flv_streams = [s for s in streams if s["type"] == "flv"][:3]
        m3u8_streams = [s for s in streams if s["type"] != "flv"][:3]
        streams = flv_streams + m3u8_streams
        return {"streams": streams[:6], "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        logging.exception("[B站] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 抖音 ====================
async def parse_douyin(url):
    try:
        live = DouyinLiveStream()
        data = await live.fetch_web_stream_data(url, process_data=True)

        qualities = ["OD", "UHD", "HD", "SD", "LD"]
        quality_names = {"OD": "原画", "UHD": "蓝光", "HD": "超清", "SD": "高清", "LD": "标清"}

        async def _fetch_quality(q):
            try:
                stream_obj = await live.fetch_stream_url(data, q)
                raw = json.loads(stream_obj.to_json())
                return q, raw.get("flv_url", ""), raw.get("m3u8_url", ""), raw.get("anchor_name", "")
            except Exception as e:
                logging.warning(f"[抖音] 画质 {q} 获取失败: {e}")
                return q, "", "", ""

        results = await asyncio.gather(*[_fetch_quality(q) for q in qualities])

        streams = []
        seen_urls = set()
        seen_qualities = set()
        anchor_name = "抖音主播"
        for q, flv, m3u8, name in results:
            if name and anchor_name == "抖音主播":
                anchor_name = name
            label = quality_names.get(q, q)
            if flv and flv not in seen_urls and label not in seen_qualities:
                seen_urls.add(flv)
                seen_qualities.add(label)
                streams.append({"cdn": f"抖音-{label}", "url": flv, "type": "flv"})
            elif m3u8 and m3u8 not in seen_urls and label not in seen_qualities:
                seen_urls.add(m3u8)
                seen_qualities.add(label)
                streams.append({"cdn": f"抖音-{label}", "url": m3u8, "type": "m3u8"})

        if not streams:
            last_logged = _DOUYIN_NO_STREAM_LOGGED.get(url, 0)
            if time.time() - last_logged > 600:
                logging.warning(f"[抖音] {url} 未获取到任何画质流")
                _DOUYIN_NO_STREAM_LOGGED[url] = time.time()
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}

        quality_order = {"原画": 0, "蓝光": 1, "超清": 2, "高清": 3, "标清": 4}
        streams.sort(key=lambda s: quality_order.get(s["cdn"].replace("抖音-", ""), 99))

        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        avatar = ""

        def find_avatar_in_dict(d, depth=0):
            if depth > 12:
                return None
            if isinstance(d, dict):
                for k, v in d.items():
                    if k in ("avatarThumb", "avatar_thumb", "avatar_larger", "avatar") and isinstance(v, dict):
                        urls = v.get("urlList") or v.get("url_list") or v.get("url")
                        if isinstance(urls, list) and urls:
                            return urls[0]
                        elif isinstance(urls, str) and urls.startswith("http"):
                            return urls
                    res = find_avatar_in_dict(v, depth + 1)
                    if res:
                        return res
            elif isinstance(d, list):
                for item in d:
                    res = find_avatar_in_dict(item, depth + 1)
                    if res:
                        return res
            return None

        try:
            avatar = find_avatar_in_dict(data) or ""
        except Exception:
            pass

        need_room_id = not room_id.isdigit()
        need_avatar_fallback = not avatar
        if need_room_id or need_avatar_fallback:
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
                page_text = resp.text
                if need_room_id:
                    match = re.search(r'"room_id":"(\d+)"', page_text)
                    if match:
                        room_id = match.group(1)
                if need_avatar_fallback:
                    m_render = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', page_text)
                    if m_render:
                        render_json = json.loads(unquote(m_render.group(1)))
                        avatar = find_avatar_in_dict(render_json) or avatar
            except Exception:
                pass

        if avatar and avatar.startswith("//"):
            avatar = "https:" + avatar

        return {"streams": streams, "title": anchor_name,
                "avatar": avatar, "roomId": room_id, "isLive": True}
    except Exception as e:
        logging.exception("[抖音] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== Twitch ====================
TWITCH_CLIENT_IDS = [
    "kimne78kx3ncx6brgo4mv6wki5h1ko",
    "ue666xxq81dq0l30715w03p3h3a6h",
    "8557m2777l2623943p9951150w621"
]

async def parse_twitch(url, cookie: str = ""):
    try:
        match = re.search(r"twitch\.tv/([^/?]+)", url)
        if not match:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        channel = match.group(1)
        eff_cookie = cookie or TWITCH_COOKIE
        auth_token = ""
        m_at = re.search(r"(?:^|;\s*)auth-token=([^;\s]+)", eff_cookie)
        if m_at:
            auth_token = m_at.group(1)

        proxylist = await get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        nickname = channel
        avatar = ""
        for client_id in TWITCH_CLIENT_IDS:
            try:
                gql_avatar_payload = [{
                    "operationName": "UserAvatar",
                    "variables": {"login": channel},
                    "query": "query UserAvatar($login: String!) { user(login: $login) { profileImageURL(width: 300) displayName } }"
                }]
                gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
                if auth_token:
                    gql_headers["Authorization"] = f"OAuth {auth_token}"
                if eff_cookie:
                    gql_headers["Cookie"] = eff_cookie
                avatar_resp = await request_with_proxy_group("POST", "https://gql.twitch.tv/gql",
                    proxy_list=proxylist, json=gql_avatar_payload, headers=gql_headers, shuffle_proxy=False,
                    log_tag="Twitch-avatar")
                if avatar_resp.status_code == 200:
                    av_data = avatar_resp.json()
                    if isinstance(av_data, list) and av_data[0].get("data", {}).get("user"):
                        user_info = av_data[0]["data"]["user"]
                        avatar = user_info.get("profileImageURL", "")
                        nickname = user_info.get("displayName", channel)
                        break
                logging.warning(f"[Twitch] Client-ID {client_id} 头像查询返回空数据")
            except Exception:
                logging.warning(f"[Twitch] Client-ID {client_id} 头像查询异常")
                continue

        token, sig = None, None
        gql_url = "https://gql.twitch.tv/gql"
        payload = [{"operationName": "PlaybackAccessToken",
                     "variables": {"login": channel, "playerType": "embed"},
                     "query": "query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: \"web\", playerType: $playerType, playerBackend: \"mediaplayer\" }) { value signature } }"}]
        for client_id in TWITCH_CLIENT_IDS:
            base_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
            if eff_cookie:
                base_headers["Cookie"] = eff_cookie
            header_variants = ([{**base_headers, "Authorization": f"OAuth {auth_token}"}] if auth_token else []) + [base_headers]
            for gql_headers in header_variants:
                try:
                    resp = await request_with_proxy_group("POST", gql_url, proxy_list=proxylist, json=payload,
                                                         headers=gql_headers, shuffle_proxy=False,
                                                         log_tag="Twitch-token")
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            t = data[0].get("data", {}).get("streamPlaybackAccessToken")
                            if t and t.get("value") and t.get("signature"):
                                token, sig = t["value"], t["signature"]
                                break
                    logging.warning(f"[Twitch] Client-ID {client_id} 失效（{'带登录' if 'Authorization' in gql_headers else '匿名'}）")
                except Exception:
                    logging.warning(f"[Twitch] Client-ID {client_id} 请求异常")
            if token and sig:
                break
        if not token or not sig:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        m3u8_url = (f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}"
                    f"&allow_source=true&allow_audio_only=true&platform=web&player=twitchweb&type=any"
                    f"&p={random.randint(100000, 999999)}&playlist_include_framerate=true&multigroup_video=true"
                    f"&supported_codecs=av1,h265,h264,mp4a&fast_bread=true")
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist,
                                                     headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"},
                                                     shuffle_proxy=False, log_tag="Twitch-m3u8")
        if usher_resp.status_code != 200 or "#EXT-X-STREAM-INF" not in usher_resp.text:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        country_m = re.search(r'USER-COUNTRY="([^"]*)"', usher_resp.text)
        if country_m:
            logging.info(f"[Twitch] usher 出口地区={country_m.group(1)} 登录={'是' if auth_token else '否'}（2K 需两者同时满足）")

        variants, lines = [], usher_resp.text.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF") or i + 1 >= len(lines):
                continue
            sub_url = lines[i + 1].strip()
            if not sub_url.startswith("http"):
                sub_url = urljoin(m3u8_url, sub_url)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            vid_m = re.search(r'VIDEO="([^"]*)"', line)
            video = vid_m.group(1) if vid_m else ""
            if res_m:
                name = f"{res_m.group(1)}p{res_m.group(2)}" + ("(源)" if video == "chunked" else "")
                variants.append((int(res_m.group(2)), name, sub_url))
            else:
                variants.append((-1, "仅音频", sub_url))
        variants.sort(key=lambda v: -v[0])
        streams = [{"cdn": f"Twitch-{name}", "url": sub_url, "type": "m3u8"} for _, name, sub_url in variants]
        if not streams:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        return {"streams": streams, "title": nickname, "avatar": avatar, "channelName": channel, "isLive": True}
    except Exception as e:
        logging.exception("[Twitch] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== SOOP ====================
def _soop_offline_ttl(bj_id: str) -> int:
    count = _SOOP_OFFLINE_COUNT.get(bj_id, 0)
    _SOOP_OFFLINE_COUNT[bj_id] = count + 1
    return min(60 * (2 ** min(count, 4)), 300)

def _soop_reset_offline(bj_id: str):
    _SOOP_OFFLINE_COUNT.pop(bj_id, None)

async def parse_soop(url, cookie: str = ""):
    try:
        eff_cookie = cookie or SOOP_COOKIE
        cache_key = url + ("_auth" if eff_cookie else "")
        cached = M3U8_CACHE.get(cache_key)
        if cached and cached["expire"] > time.time():
            return cached["data"]

        parts = url.rstrip('/').split('/')
        bj_id = parts[3].split('?')[0] if len(parts) > 3 else parts[-1].split('?')[0]
        if not bj_id:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}

        headers_pc = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://play.sooplive.com',
            'referer': 'https://play.sooplive.com',
        }
        if eff_cookie:
            headers_pc['cookie'] = eff_cookie

        proxylist = await get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        nickname = f'BJ-{bj_id}'
        avatar = f'https://stimg.sooplive.com/LOGO/{bj_id[:2]}/{bj_id}/{bj_id}.jpg'

        def _fail(nick, av):
            result = {"streams": [], "isLive": False, "title": nick, "avatar": av}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result

        live_api = f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}'
        live_data_form = {
            'bid': bj_id, 'bno': '', 'type': '', 'pwd': '',
            'player_type': 'html5', 'stream_type': 'common', 'quality': 'master',
            'mode': 'landing', 'from_api': '0', 'is_revive': 'false',
        }
        live_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                   headers=headers_pc, data=live_data_form, shuffle_proxy=False,
                                                   log_tag="SOOP-live")
        if live_resp.status_code != 200:
            return _fail(nickname, avatar)
        live_json = live_resp.json()
        channel = live_json.get('CHANNEL', {})
        nickname = channel.get('BJ_NM') or channel.get('BJNICK') or nickname
        result_code = channel.get('RESULT', -1)
        if result_code == -6:
            return {"streams": [], "isLive": False, "error": "19+成年直播间，请在设置中填入SOOP登录Cookie",
                    "title": nickname, "avatar": avatar}
        if result_code not in [0, 1]:
            return _fail(nickname, avatar)

        broad_no = channel.get('BNO', '')
        if not broad_no:
            return _fail(nickname, avatar)

        ts_now = time.time()
        cdn_params = {
            'return_type': 'gcp_cdn',
            'use_cors': 'false',
            'cors_origin_url': 'play.sooplive.com',
            'broad_key': f'{broad_no}-common-master-hls',
            'time': str(ts_now),
        }
        cdn_resp = await request_with_proxy_group("GET",
            'http://livestream-manager.sooplive.com/broad_stream_assign.html',
            proxy_list=proxylist, headers=headers_pc, params=cdn_params, shuffle_proxy=False,
            log_tag="SOOP-cdn")
        if cdn_resp.status_code != 200:
            return _fail(nickname, avatar)
        cdn_json = cdn_resp.json()
        view_url = cdn_json.get('view_url')
        if not view_url:
            return _fail(nickname, avatar)

        aid_form = live_data_form.copy()
        aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                  headers=headers_pc, data=aid_form, shuffle_proxy=False,
                                                  log_tag="SOOP-aid")
        if aid_resp.status_code != 200:
            return _fail(nickname, avatar)
        aid_json = aid_resp.json()
        aid = aid_json.get('CHANNEL', {}).get('AID', '')
        if not aid:
            return _fail(nickname, avatar)

        m3u8_url = f'{view_url}?aid={aid}'
        streams = []
        try:
            master_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist,
                                                         headers={"User-Agent": UA, "Referer": "https://play.sooplive.com"},
                                                         shuffle_proxy=False, log_tag="SOOP-master")
            if master_resp.status_code == 200:
                streams = parse_multivariant_m3u8(master_resp.text, m3u8_url, "SOOP")
        except Exception:
            pass
        if not streams:
            streams = [{"cdn": "SOOP-Source", "url": m3u8_url, "type": "m3u8"}]

        result = {"streams": streams, "title": nickname, "avatar": avatar, "isLive": True,
                   "danmaku": {"platform": "soop", "roomId": bj_id}}
        _soop_reset_offline(bj_id)
        M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 15}
        return result
    except Exception as e:
        logging.exception("[SOOP] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== PandaTV ====================
async def parse_panda_manual(url):
    try:
        cached = M3U8_CACHE.get(url)
        if cached and cached["expire"] > time.time():
            return cached["data"]

        user_id = url.split('?')[0].rstrip('/').split('/')[-1]
        headers = {'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/', 'user-agent': UA}
        proxylist = await get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        info_url = 'https://api.pandalive.co.kr/v1/member/bj'
        resp = await request_with_proxy_group("POST", info_url, proxy_list=proxylist,
                                              headers=headers,
                                              data={'userId': user_id, 'info': 'media fanGrade'},
                                              shuffle_proxy=False, log_tag="PandaTV-info")
        if resp.status_code != 200:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        info_json = resp.json()
        if 'bjInfo' not in info_json:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        bj_info = info_json.get('bjInfo', {})
        anchor_name = bj_info.get('nick', user_id)

        _IMG_FIELDS = (
            'thumbUrl', 'profileImg', 'profileImage', 'img', 'userImg', 'thumbImg',
            'bjImg', 'thumb', 'photo', 'avatar', 'iconImg', 'userPic',
            'thumbnail', 'profile', 'profileThumb',
        )
        avatar = next((bj_info[k] for k in _IMG_FIELDS if bj_info.get(k)), '')

        if 'media' not in info_json:
            result = {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
            M3U8_CACHE[url] = {"data": result, "expire": time.time() + 60}
            return result

        play_url = 'https://api.pandalive.co.kr/v1/live/play'
        resp2 = await request_with_proxy_group("POST", play_url, proxy_list=proxylist,
                                               headers=headers,
                                               data={'action': 'watch', 'userId': user_id, 'password': '', 'shareLinkType': ''},
                                               shuffle_proxy=False, log_tag="PandaTV-play")
        if resp2.status_code != 200:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        play_json = resp2.json()
        if 'errorData' in play_json:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        if 'PlayList' not in play_json or 'hls' not in play_json['PlayList']:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        real_m3u8 = play_json['PlayList']['hls'][0]['url']

        if not avatar or 'default' in avatar.lower() or 'no_image' in avatar.lower():
            for _section in ('bjInfo', 'userInfo', 'bjProfile', 'channelInfo', 'mediaInfo'):
                _d = play_json.get(_section)
                if isinstance(_d, dict):
                    _candidate = next((str(_d[k]) for k in _IMG_FIELDS if _d.get(k)), '')
                    if _candidate:
                        avatar = _candidate
                        logging.info(f"[PandaTV] 从 play_json[{_section}] 获取到头像")
                        break

        def _fix_avatar_url(u):
            if not u: return ''
            if u.startswith('//'): return 'https:' + u
            if not u.startswith('http'):
                return 'https://profile.pandalive.co.kr' + ('/' if not u.startswith('/') else '') + u
            return u
        avatar = _fix_avatar_url(avatar)

        streams = []
        try:
            cf_worker = CF_WORKER.rstrip("/") if CF_WORKER else ""
            if cf_worker:
                fetch_url = f"{cf_worker}?url={quote(real_m3u8, safe='')}&referer={quote('https://www.pandalive.co.kr/', safe='')}"
                master_resp = await request_with_proxy_group("GET", fetch_url, proxy_list=[None],
                                                             headers={"User-Agent": UA}, shuffle_proxy=False,
                                                             log_tag="PandaTV-master")
            else:
                master_resp = await request_with_proxy_group("GET", real_m3u8, proxy_list=proxylist,
                                                             headers={"User-Agent": UA, "Referer": "https://www.pandalive.co.kr/",
                                                                      "Origin": "https://www.pandalive.co.kr"},
                                                             shuffle_proxy=False, log_tag="PandaTV-master")
            if master_resp.status_code == 200:
                streams = parse_multivariant_m3u8(master_resp.text, real_m3u8, "PandaTV")
        except Exception:
            pass
        if not streams:
            streams = [{"cdn": "PandaTV-Source", "url": real_m3u8, "type": "m3u8"}]

        result = {"streams": streams, "title": anchor_name, "avatar": avatar, "isLive": True}
        M3U8_CACHE[url] = {"data": result, "expire": time.time() + 15}
        return result
    except Exception as e:
        logging.exception("[PandaTV] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

@app.post("/api/follows/batch")
@limiter.limit("20/minute")
async def api_follows_batch(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")

    items = body.get("items", [])
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "items 不能为空")
    if len(items) > 30:
        raise HTTPException(400, "单次最多查询 30 个")

    _FOREIGN_MARKERS = ("twitch.tv", "sooplive.com", "pandalive.co.kr")
    sem_domestic = asyncio.Semaphore(5)
    sem_foreign = asyncio.Semaphore(5)

    async def _one(item):
        url = item.get("url", "")
        cookie = item.get("cookie", "")
        sem = sem_foreign if any(m in url for m in _FOREIGN_MARKERS) else sem_domestic
        async with sem:
            try:
                result = await _parse_dispatch(url, cookie)
            except HTTPException as e:
                result = {"streams": [], "isLive": False, "title": "", "avatar": "", "error": str(e.detail)}
            except Exception as e:
                result = {"streams": [], "isLive": False, "title": "", "avatar": "", "error": str(e)}
        result["url"] = url
        return result

    results = await asyncio.gather(*(_one(it) for it in items))
    return {"results": results}

@app.get("/api/parse")
@limiter.limit("60/minute")
async def api_parse(request: Request, url: str = Query(...), cookie: str = Query("")):
    try:
        return await _parse_dispatch(url, cookie)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

async def _parse_dispatch(url: str, cookie: str = ""):
    try:
        if "huya.com" in url: return await parse_huya(url)
        if "douyu.com" in url: return await parse_douyu(url)
        if "bilibili.com" in url: return await parse_bilibili(url)
        if "douyin.com" in url: return await parse_douyin(url)
        if "twitch.tv" in url: return await parse_twitch(url, cookie=cookie)
        if "sooplive.com" in url: return await parse_soop(url, cookie=cookie)
        if "pandalive.co.kr" in url: return await parse_panda_manual(url)
        raise HTTPException(400, "不支持的平台")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ==================== 弹幕代理 ====================

def _reset_go_service_check(mark_unavailable: bool = False):
    global _GO_SERVICE_AVAILABLE, _GO_SERVICE_LAST_CHECK
    _GO_SERVICE_LAST_CHECK = 0
    if mark_unavailable:
        _GO_SERVICE_AVAILABLE = False

def _ws_origin_allowed(websocket: WebSocket) -> bool:
    if not ALLOWED_WS_ORIGINS:
        return True
    origin = websocket.headers.get("origin", "")
    if not origin:
        return True
    return origin in ALLOWED_WS_ORIGINS

@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    if not _ws_origin_allowed(websocket):
        logging.warning(f"[WS] 抖音弹幕拒绝非法 Origin: {websocket.headers.get('origin')!r}")
        await websocket.close(code=1008)
        return

    await websocket.accept()

    if not await _check_go_service():
        try:
            await websocket.send_text(json.dumps({
                "method": "error",
                "content": "弹幕服务暂时不可用，请稍后重试"
            }))
            await websocket.close(code=1013)
        except Exception:
            pass
        return

    go_ws_url = f"{GO_DANMAKU_BASE}/ws/{room_id}"
    try:
        async with websockets.connect(go_ws_url, ping_interval=None) as go_ws:
            async def forward_to_go():
                try:
                    while True:
                        data = await websocket.receive_text()
                        if data == 'ping':
                            try:
                                await go_ws.ping()
                            except Exception:
                                pass
                            await websocket.send_text('pong')
                        elif go_ws.state.name == 'OPEN':
                            await go_ws.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logging.debug(f"[WS] forward_to_go 异常: {e}")

            async def forward_to_frontend():
                try:
                    while True:
                        data = await asyncio.wait_for(go_ws.recv(), timeout=60)
                        if isinstance(data, bytes):
                            data = data.decode("utf-8")
                        await websocket.send_text(data)
                except asyncio.TimeoutError:
                    _reset_go_service_check()
                    logging.warning(f"[WS] 从 Go 服务接收超时 (room={room_id})，关闭连接")
                except Exception as e:
                    logging.debug(f"[WS] forward_to_frontend 异常: {e}")

            task_go = asyncio.create_task(forward_to_go())
            task_fe = asyncio.create_task(forward_to_frontend())
            done, pending = await asyncio.wait(
                [task_go, task_fe],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    except websockets.exceptions.ConnectionClosed:
        logging.info(f"[WS] Go 服务连接已关闭 (room={room_id})")
        _reset_go_service_check()
    except OSError as e:
        _reset_go_service_check(mark_unavailable=True)
        logging.warning(f"[WS] 抖音代理无法连接 Go 服务: {e}")
    except Exception as e:
        logging.warning(f"[WS] 抖音代理异常: {e}")
    finally:
        try:
            if websocket.client_state.name == 'OPEN':
                await websocket.close()
        except Exception:
            pass

_SOOP_ESC = b'\x1b\x09'
_SOOP_F = b'\x0c'


def _soop_build_frame(service: int, body: bytes) -> bytes:
    header = _SOOP_ESC + f"{service:04d}".encode("ascii") + f"{len(body):06d}".encode("ascii") + b"00"
    return header + body


_SOOP_ELEM_START = b'\x11'
_SOOP_ELEM_END = b'\x12'
_SOOP_SPACE = b'\x06'


def _soop_cookie_field(cookie: str, name: str) -> str:
    if not cookie:
        return ""
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def _soop_connect_packet(auth_ticket: str = "") -> bytes:
    if auth_ticket:
        body = _SOOP_F + auth_ticket.encode("utf-8") + _SOOP_F * 2 + b"16" + _SOOP_F
    else:
        body = _SOOP_F * 3 + b"16" + _SOOP_F
    return _soop_build_frame(1, body)


def _soop_log_query(meta: dict) -> bytes:
    out = b""
    for k, v in meta.items():
        out += _SOOP_SPACE + b"&" + _SOOP_SPACE + str(k).encode("utf-8") + _SOOP_SPACE + b"=" + _SOOP_SPACE + str(v).encode("utf-8")
    return out


def _soop_join_packet(chat_no: str, ftk: str = "", _au: str = "", log_meta: dict = None) -> bytes:
    chat_no_bytes = chat_no.encode("utf-8")
    if ftk:
        meta = log_meta or {}
        query = _soop_log_query({
            "set_bps": meta.get("set_bps", "0"),
            "view_bps": meta.get("view_bps", "0"),
            "quality": "normal",
            "uuid": _au or "",
            "geo_cc": meta.get("geo_cc", ""),
            "geo_rc": meta.get("geo_rc", ""),
            "acpt_lang": meta.get("acpt_lang", ""),
            "svc_lang": meta.get("svc_lang", ""),
            "subscribe": 0,
            "lowlatency": 0,
            "mode": "landing",
        })
        body = (_SOOP_F + chat_no_bytes + _SOOP_F + ftk.encode("utf-8") + _SOOP_F + b"0" + _SOOP_F
                + b"log" + _SOOP_ELEM_START + query + _SOOP_ELEM_END
                + b"pwd" + _SOOP_ELEM_START + _SOOP_ELEM_END
                + b"auth_info" + _SOOP_ELEM_START + b"NULL" + _SOOP_ELEM_END
                + b"pver" + _SOOP_ELEM_START + b"2" + _SOOP_ELEM_END
                + b"access_system" + _SOOP_ELEM_START + b"html5" + _SOOP_ELEM_END
                + _SOOP_F)
    else:
        body = _SOOP_F + chat_no_bytes + _SOOP_F * 5
    return _soop_build_frame(2, body)


def _soop_enter_info_packet(syn_ack: str) -> bytes:
    body = _SOOP_F + syn_ack.encode("utf-8") + _SOOP_F + b"0" + _SOOP_F
    return _soop_build_frame(12, body)


def _soop_heartbeat_packet() -> bytes:
    return _soop_build_frame(0, _SOOP_F)


def _soop_parse_frames(data: bytes):
    frames = []
    offset = 0
    header_len = 14
    while offset + header_len <= len(data):
        if data[offset:offset + 2] != _SOOP_ESC:
            break
        try:
            service = int(data[offset + 2:offset + 6].decode("ascii", errors="ignore"))
            body_len = int(data[offset + 6:offset + 12].decode("ascii", errors="ignore"))
        except ValueError:
            break
        if body_len < 0:
            break
        packet_end = offset + header_len + body_len
        if packet_end > len(data):
            break
        frames.append((service, data[offset + header_len:packet_end]))
        offset = packet_end
    return frames


def _soop_decode_chat(body: bytes):
    parts = body.split(_SOOP_F)
    fields = [p.decode("utf-8", errors="ignore") for p in parts]
    if len(fields) <= 6:
        return None
    comment = fields[1].strip()
    nick = fields[6].strip()
    if not comment or not nick or comment in ("-1", "1") or "|" in comment:
        return None
    return nick, comment


@app.websocket("/ws/twitch/{channel_name}")
async def websocket_twitch_danmaku(ws_conn: WebSocket, channel_name: str):
    if not _ws_origin_allowed(ws_conn):
        logging.warning(f"[Twitch WS] 拒绝非法 Origin: {ws_conn.headers.get('origin')!r}")
        await ws_conn.close(code=1008)
        return

    await ws_conn.accept()
    twitch_ws_url = "wss://irc-ws.chat.twitch.tv:443"
    try:
        async with websockets.connect(twitch_ws_url, ping_interval=30, ping_timeout=10) as twitch_ws:
            await twitch_ws.send("PASS SCHMOOPIIE")
            await twitch_ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await twitch_ws.send(f"JOIN #{channel_name.lower()}")

            async def forward_to_frontend():
                try:
                    async for msg in twitch_ws:
                        if msg.startswith("PING"):
                            await twitch_ws.send("PONG :tmi.twitch.tv")
                            continue
                        m = re.match(r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)", msg)
                        if m:
                            try:
                                await ws_conn.send_json({"type": "chat", "nick": m.group(1), "content": m.group(2)})
                            except Exception:
                                break
                except Exception as e:
                    logging.debug(f"[Twitch WS] forward_to_frontend 异常: {e}")

            async def listen_to_frontend():
                try:
                    while True:
                        data = await ws_conn.receive_text()
                        if data == "ping":
                            try:
                                await ws_conn.send_text("pong")
                            except Exception:
                                break
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logging.debug(f"[Twitch WS] listen_to_frontend 异常: {e}")

            task_fwd = asyncio.create_task(forward_to_frontend())
            task_lsn = asyncio.create_task(listen_to_frontend())
            done, pending = await asyncio.wait(
                [task_fwd, task_lsn],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        logging.warning(f"[Twitch WS] 连接异常: {e}")
    finally:
        try:
            await ws_conn.close()
        except Exception:
            pass

@app.websocket("/ws/soop/{room_id}")
async def websocket_soop_danmaku(ws_conn: WebSocket, room_id: str, cookie: str = Query("")):
    if not _ws_origin_allowed(ws_conn):
        logging.warning(f"[SOOP WS] 拒绝非法 Origin: {ws_conn.headers.get('origin')!r}")
        await ws_conn.close(code=1008)
        return

    await ws_conn.accept()
    try:
        headers_pc = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://play.sooplive.com',
            'referer': 'https://play.sooplive.com',
        }
        if cookie:
            headers_pc['cookie'] = cookie
        live_api = f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={room_id}'
        live_data_form = {
            'bid': room_id, 'bno': '', 'type': '', 'pwd': '',
            'player_type': 'html5', 'stream_type': 'common', 'quality': 'master',
            'mode': 'landing', 'from_api': '0', 'is_revive': 'false',
        }
        proxylist = await get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        live_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                     headers=headers_pc, data=live_data_form, shuffle_proxy=False,
                                                     log_tag="SOOP-danmaku-meta")
        channel = live_resp.json().get('CHANNEL', {})
        chat_no = str(channel.get('CHATNO', '')).strip()
        auth_ticket = _soop_cookie_field(cookie, 'AuthTicket')
        au = _soop_cookie_field(cookie, '_au')
        ftk = str(channel.get('FTK', '') or '').strip()
        view_bps = '0'
        try:
            presets = channel.get('VIEWPRESET')
            if isinstance(presets, list) and presets and isinstance(presets[0], dict):
                view_bps = str(presets[0].get('bps', '0') or '0')
        except Exception:
            pass
        log_meta = {
            'set_bps': str(channel.get('BPS', '0') or '0'),
            'view_bps': view_bps,
            'geo_cc': str(channel.get('geo_cc', '') or ''),
            'geo_rc': str(channel.get('geo_rc', '') or ''),
            'acpt_lang': str(channel.get('acpt_lang', '') or ''),
            'svc_lang': str(channel.get('svc_lang', '') or ''),
        }
        if not chat_no:
            msg = "SOOP 聊天室号获取失败"
            if not cookie:
                msg += "：若为 19+ 直播间，请在设置页填写 SOOP 登录 Cookie 后重试"
            await ws_conn.send_json({"type": "error", "message": msg})
            await ws_conn.close()
            return

        host = str(channel.get('CHDOMAIN', '') or '').strip()
        if not host:
            raw_ip = str(channel.get('CHIP', '') or '').strip()
            octets = raw_ip.split('.')
            if len(octets) == 4 and all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
                encoded = ''.join(f'{int(o):02X}' for o in octets)
                host = f'chat-{encoded}.sooplive.com'
        try:
            plain_port = int(channel.get('CHPT', 0))
        except (ValueError, TypeError):
            plain_port = 0
        if not host or plain_port <= 0 or plain_port >= 65535:
            await ws_conn.send_json({"type": "error", "message": "SOOP 弹幕连接地址解析失败"})
            await ws_conn.close()
            return

        soop_ws_url = f"wss://{host}:{plain_port + 1}/Websocket/{room_id}"
        logging.info(f"[SOOP WS] room={room_id} chatNo={chat_no} host={host} plainPort={plain_port} "
                     f"auth={'on' if auth_ticket else 'off'} ftk={'y' if ftk else 'n'} → {soop_ws_url}")
        ws_headers = {
            "Origin": "https://play.sooplive.co.kr",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        }
        if cookie:
            ws_headers["Cookie"] = cookie

        try:
            soop_ws_ctx = websockets.connect(
                soop_ws_url, subprotocols=["chat"], additional_headers=ws_headers,
                ping_interval=None, open_timeout=10,
            )
        except TypeError:
            soop_ws_ctx = websockets.connect(
                soop_ws_url, subprotocols=["chat"], extra_headers=ws_headers,
                ping_interval=None, open_timeout=10,
            )

        async with soop_ws_ctx as soop_ws:
            logging.info(f"[SOOP WS] room={room_id} 已连接到 SOOP chat 服务器，开始发送握手包")
            await soop_ws.send(_soop_connect_packet(auth_ticket))
            await asyncio.sleep(0.2)
            await soop_ws.send(_soop_join_packet(chat_no, ftk=ftk if auth_ticket else "", _au=au, log_meta=log_meta))
            logging.info(f"[SOOP WS] room={room_id} 握手包+加入包已发送（{'认证' if auth_ticket else '匿名'}模式）")

            async def heartbeat():
                try:
                    while True:
                        await asyncio.sleep(20)
                        await soop_ws.send(_soop_heartbeat_packet())
                except Exception as e:
                    logging.warning(f"[SOOP WS] room={room_id} heartbeat 异常 [{type(e).__name__}]: {e}")

            _frame_count = 0
            _chat_count = 0

            async def forward_to_frontend():
                nonlocal _frame_count, _chat_count
                try:
                    async for msg in soop_ws:
                        if isinstance(msg, str):
                            logging.info(f"[SOOP WS] room={room_id} 收到文本帧(忽略): {msg[:200]!r}")
                            continue
                        _frame_count += 1
                        for service, body in _soop_parse_frames(msg):
                            if service == 2 and auth_ticket:
                                parts2 = body.split(_SOOP_F)
                                syn_ack = parts2[7].decode('utf-8', errors='ignore').strip() if len(parts2) > 7 else ''
                                if syn_ack:
                                    try:
                                        await soop_ws.send(_soop_enter_info_packet(syn_ack))
                                    except Exception:
                                        pass
                                continue
                            if service != 5:
                                continue
                            decoded = _soop_decode_chat(body)
                            if not decoded:
                                continue
                            _chat_count += 1
                            nick, comment = decoded
                            try:
                                await ws_conn.send_json({"type": "chat", "nick": nick, "content": comment})
                            except Exception:
                                return
                    close_code = getattr(soop_ws, "close_code", None)
                    close_reason = getattr(soop_ws, "close_reason", None)
                    logging.warning(
                        f"[SOOP WS] room={room_id} 连接被对端关闭（正常结束，无异常）。"
                        f"close_code={close_code} close_reason={close_reason!r} "
                        f"共收到二进制帧={_frame_count} 解析出弹幕={_chat_count}"
                    )
                except Exception as e:
                    logging.warning(f"[SOOP WS] room={room_id} forward_to_frontend 异常 [{type(e).__name__}]: {e}")

            async def listen_to_frontend():
                try:
                    while True:
                        data = await ws_conn.receive_text()
                        if data == "ping":
                            try:
                                await ws_conn.send_text("pong")
                            except Exception:
                                break
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logging.debug(f"[SOOP WS] listen_to_frontend 异常: {e}")

            task_hb = asyncio.create_task(heartbeat())
            task_fwd = asyncio.create_task(forward_to_frontend())
            task_lsn = asyncio.create_task(listen_to_frontend())
            done, pending = await asyncio.wait(
                [task_hb, task_fwd, task_lsn],
                return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        logging.warning(f"[SOOP WS] room={room_id} 连接异常 [{type(e).__name__}]: {e}")
    finally:
        try:
            await ws_conn.close()
        except Exception:
            pass


@app.get("/")
def root():
    return {"status": "ok"}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    proxies = []
    for proxy, h in _PROXY_HEALTH.items():
        stream_ok = sum(v["success"] for t, v in h["by_tag"].items() if t in _STREAM_TAGS)
        stream_fail = sum(v["fail"] for t, v in h["by_tag"].items() if t in _STREAM_TAGS)
        is_bad = stream_fail > 0 and stream_fail >= stream_ok
        proxies.append({
            "proxy": proxy,
            "status": "bad" if is_bad else "ok",
            "stream_success": stream_ok,
            "stream_fail": stream_fail,
        })
    proxies.sort(key=lambda p: p["status"] != "bad")

    pool_info = {k: {"idle_sec": round(time.time() - ts)} for k, (_, ts) in CLIENT_POOL.items()}

    return {
        "status": "alive",
        "proxies": proxies,
        "go_danmaku": {
            "available": _GO_SERVICE_AVAILABLE,
            "endpoint": GO_DANMAKU_BASE,
        },
        "connection_pool": pool_info,
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
