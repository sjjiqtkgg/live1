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
# 【修复】用 Lock 串行化探测协程，防止 Go 不可用时多个 WS 连接并发触发探测风暴
_GO_SERVICE_CHECK_LOCK = asyncio.Lock()

async def _check_go_service() -> bool:
    """TCP 探测 Go 弹幕服务是否在线，结果写入全局标志。
    【修复1】冷却判断改为：30 秒内无论可用与否都直接返回缓存结果，不重探。
    【修复2】用 asyncio.Lock 确保同一时刻只有一个协程在做 TCP 探测，
            消除多 WS 并发时的探测风暴。
    """
    global _GO_SERVICE_AVAILABLE, _GO_SERVICE_LAST_CHECK
    now = time.time()
    # 快速路径：冷却期内直接返回上次结果，不需要加锁
    if now - _GO_SERVICE_LAST_CHECK < _GO_SERVICE_CHECK_INTERVAL:
        return _GO_SERVICE_AVAILABLE
    # 慢路径：需要真正探测，加锁串行化
    async with _GO_SERVICE_CHECK_LOCK:
        # 双重检查：拿到锁时可能已经被前一个协程探测过了
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
    # 【修复3】启动时探测 Go 服务
    await _check_go_service()

    # 【修复1/2】记录连接池上次清理时间，避免依赖 time % interval 导致启动即清理
    last_pool_rebuild = time.time()
    last_health_reset = time.time()

    async def _cache_cleanup():
        nonlocal last_pool_rebuild, last_health_reset
        while True:
            await asyncio.sleep(60)
            now = time.time()

            # 清理 M3U8 缓存
            expired = [k for k, v in list(M3U8_CACHE.items()) if v.get('expire', 0) < now]
            for k in expired:
                M3U8_CACHE.pop(k, None)
            if expired:
                logging.info(f"[缓存] 清理 {len(expired)} 条过期条目，剩余 {len(M3U8_CACHE)} 条")

            # 【修复4】清理 STREAM_PROXY_MAP 时跳过距上次写入不足 60 秒的 key，
            # 避免误删正在服务中的 TS 切片流映射。
            if len(STREAM_PROXY_MAP) > 500:
                now_ts = time.time()
                # stream_proxy_map_ts 存储每个 key 的写入时间
                safe_to_remove = [
                    k for k in list(STREAM_PROXY_MAP.keys())
                    if now_ts - STREAM_PROXY_MAP_TS.get(k, 0) > 60
                ]
                for k in safe_to_remove[:250]:
                    STREAM_PROXY_MAP.pop(k, None)
                    STREAM_PROXY_MAP_TS.pop(k, None)
                logging.info(f"[缓存] STREAM_PROXY_MAP 超限，已安全清理 {len(safe_to_remove[:250])} 条，剩余 {len(STREAM_PROXY_MAP)} 条")

            # 清理 SOOP 离线退避计数器
            if len(_SOOP_OFFLINE_COUNT) > 1000:
                keys = list(_SOOP_OFFLINE_COUNT.keys())
                for k in keys[:500]:
                    _SOOP_OFFLINE_COUNT.pop(k, None)

            # 清理抖音"无画质流"日志去重记录
            if len(_DOUYIN_NO_STREAM_LOGGED) > 1000:
                keys = list(_DOUYIN_NO_STREAM_LOGGED.keys())
                for k in keys[:500]:
                    _DOUYIN_NO_STREAM_LOGGED.pop(k, None)

            # 【修复1】连接池惰性清理：每小时清理空闲超过 15 分钟的连接，
            # 而不是一次性全部销毁，避免误杀正在进行 I/O 的客户端。
            if now - last_pool_rebuild >= 3600:
                last_pool_rebuild = now
                async with CLIENT_LOCK:
                    idle_keys = [
                        k for k, (client, last_use) in list(CLIENT_POOL.items())
                        if now - last_use > 900  # 空闲超过 15 分钟
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

            # 每 6 小时重置代理健康统计
            if now - last_health_reset >= 6 * 3600:
                last_health_reset = now
                if _PROXY_HEALTH:
                    logging.info(f"[代理健康] 周期性重置统计，重置前共 {len(_PROXY_HEALTH)} 个代理记录")
                    _PROXY_HEALTH.clear()

            # 【修复3】定期重探 Go 服务可用性
            if not _GO_SERVICE_AVAILABLE:
                await _check_go_service()

    task = asyncio.create_task(_cache_cleanup())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 关闭所有连接池中的客户端
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

def get_real_ip(request: Request) -> str:
    """Render 等反代环境下，request.client.host 是负载均衡器内网 IP，
    所有用户会共享同一限流计数器。优先读取 X-Forwarded-For 第一段。"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# ==================== API 限流 ====================
if SLOWAPI_AVAILABLE:
    limiter = Limiter(key_func=get_real_ip)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
else:
    logging.warning("[限流] slowapi 未安装，/api/parse 与 /api/proxy 不受限流保护，建议 pip install slowapi")
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
# 【修复1】CLIENT_POOL 存储 (client, last_use_ts) 元组，以便惰性清理时判断空闲时长
CLIENT_POOL: dict = {}   # key -> (httpx.AsyncClient, last_use_timestamp)
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15

async def get_client(proxy=None, timeout=None):
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    # 【修复1】先在锁外做快速路径检查；读 tuple[0] 是原子操作，不会撕裂
    entry = CLIENT_POOL.get(key)
    if entry is not None:
        client, _ = entry
        # 更新最后使用时间（允许并发写，Python dict 赋值是 GIL 保护的原子操作）
        CLIENT_POOL[key] = (client, time.time())
        return client
    async with CLIENT_LOCK:
        # 双重检查，防止并发时重复创建
        entry = CLIENT_POOL.get(key)
        if entry is not None:
            client, _ = entry
            CLIENT_POOL[key] = (client, time.time())
            return client
        client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy,
            http2=True,
            verify=False,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20
            )
        )
        CLIENT_POOL[key] = (client, time.time())
        return client

STREAM_PROXY_MAP: dict = {}       # stream_key -> proxy
STREAM_PROXY_MAP_TS: dict = {}    # 【修复4】stream_key -> 写入时间戳，用于安全清理
M3U8_CACHE: dict = {}
_SOOP_OFFLINE_COUNT: dict = {}    # { bj_id: 连续离线检测次数 }
_DOUYIN_NO_STREAM_LOGGED: dict = {}  # { url: last_log_ts }

# ==================== 代理健康统计（用于 /health 监控） ====================
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

# 拉流相关的 tag 前缀，用于 /health 里单独高亮"能不能拉到流"
_STREAM_TAGS = ("SOOP-live", "SOOP-cdn", "SOOP-master", "SOOP-aid",
                 "Twitch-m3u8", "Twitch-token", "PandaTV-play", "PandaTV-master")

def _short_url(url: str, length: int = 60) -> str:
    """截断 URL 用于日志展示，保留协议+域名部分，避免截成残缺的 'p://...'。"""
    path = url.split('?')[0]
    return path if len(path) <= length else path[:length] + "…"

# 【修复2】用 itertools.cycle + asyncio.Lock 实现真正线程安全的轮询分配
# 当代理列表变化时（目前不会，保留扩展性），重建 cycle 即可。
_PROXY_RR_LOCK = asyncio.Lock()
_PROXY_RR_CYCLES: dict = {}   # pool_id -> itertools.cycle

def _get_rr_cycle(proxy_pool):
    """获取/初始化指定代理池的 cycle 对象。"""
    pool_id = id(proxy_pool)
    if pool_id not in _PROXY_RR_CYCLES:
        _PROXY_RR_CYCLES[pool_id] = itertools.cycle(range(len(proxy_pool)))
    return _PROXY_RR_CYCLES[pool_id]

async def get_fixed_proxy_list(proxy_pool):
    """轮询分配代理，返回该次调用要尝试的代理优先级列表（首位优先，其余作为 fallback）。
    【修复2】用 asyncio.Lock 保护 cycle.next()，确保并发下轮询严格均匀。
    【修复2】增加空列表和 [None] 的防御性检查，避免 IndexError / ZeroDivisionError。"""
    if not proxy_pool:
        return [None]
    if len(proxy_pool) == 1 and proxy_pool[0] is None:
        return [None]
    n = len(proxy_pool)
    async with _PROXY_RR_LOCK:
        cycle = _get_rr_cycle(proxy_pool)
        start = next(cycle)
    return [proxy_pool[(start + i) % n] for i in range(n)]

async def request_with_retry(method, url, **kwargs):
    """顺序遍历国内代理池，失败后间隔 0.5s 重试下一个。"""
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    log_tag = kwargs.pop("log_tag", None)
    for proxy in PROXY_URLS:
        try:
            client = await get_client(proxy, timeout)
            resp = await client.request(method, url, **kwargs)
            _record_proxy_health(proxy, True, tag=log_tag)
            return resp
        except Exception as e:
            _record_proxy_health(proxy, False, tag=log_tag, error=f"{type(e).__name__}: {e}")
            last_error = e
            logging.warning(f"[请求重试]{f'[{log_tag}]' if log_tag else ''} {proxy or '直连'} 失败 [{type(e).__name__}]: {e}")
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    """顺序遍历指定代理列表，失败后间隔 0.5s 重试下一个。"""
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    shuffle_proxy = kwargs.pop("shuffle_proxy", False)
    log_tag = kwargs.pop("log_tag", None)

    targets = proxy_list[:]
    if shuffle_proxy and targets:
        random.shuffle(targets)

    for proxy in targets:
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
            logging.warning(f"[分组请求]{f'[{log_tag}]' if log_tag else ''} {proxy or '直连'} 失败 [{type(e).__name__}]: {e}")
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")


async def request_race(method, url, proxy_list, **kwargs):
    """并发所有代理，取最快成功的响应并取消其余。
    仅适用于 API/M3U8 等小负载请求，勿用于 TS 切片。"""
    timeout = kwargs.pop("timeout", 15)
    log_tag = kwargs.pop("log_tag", None)
    kwargs.pop("shuffle_proxy", None)

    if not proxy_list:
        proxy_list = [None]

    # 单代理直接请求，无需竞速
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
        # 【修复4】写入 STREAM_PROXY_MAP 时同步记录时间戳
        if stream_key not in STREAM_PROXY_MAP:
            if proxy_list and proxy_list[0] is not None:
                STREAM_PROXY_MAP[stream_key] = random.choice(proxy_list)
            else:
                STREAM_PROXY_MAP[stream_key] = None
            STREAM_PROXY_MAP_TS[stream_key] = time.time()
        else:
            # 每次访问刷新时间戳，避免活跃流被误清理
            STREAM_PROXY_MAP_TS[stream_key] = time.time()
        fixed_proxy = STREAM_PROXY_MAP[stream_key]
        proxies_to_use = [fixed_proxy] if fixed_proxy else proxy_list
        shuffle_proxy = False
    else:
        proxies_to_use = proxy_list
        shuffle_proxy = True

    resp = await request_with_proxy_group(
        request.method, url,
        proxy_list=proxies_to_use,
        headers=headers, content=body,
        shuffle_proxy=shuffle_proxy
    )
    content_type = resp.headers.get("content-type", "")
    is_m3u8 = "mpegurl" in content_type.lower() or url.split("?")[0].endswith(".m3u8")

    if is_m3u8:
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
        lines = resp.text.splitlines()
        rewritten = []
        for line in lines:
            stripped = line.strip()
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
        # 【修复4（TS流连接释放）】使用异步生成器确保 resp 在流传输结束或客户端断开后被关闭，
        # 释放 httpx keepalive 连接，避免连接池耗尽。
        async def _stream_and_close():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                try:
                    await resp.aclose()
                except Exception:
                    pass

        return StreamingResponse(
            _stream_and_close(),
            status_code=resp.status_code,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": content_type or "video/mp2t"
            }
        )
    out_headers = {"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "application/json"}
    return StreamingResponse(iter([resp.content]), status_code=resp.status_code, headers=out_headers)


def build_streams(flv, m3u8):
    s = []
    if flv and flv.startswith("http"):
        s.append({"cdn": "FLV", "url": flv, "type": "flv"})
    if m3u8 and m3u8.startswith("http"):
        s.append({"cdn": "HLS", "url": m3u8, "type": "m3u8"})
    return s

def parse_multivariant_m3u8(text, base_url, cdn_prefix):
    """解析 HLS multivariant playlist，提取各画质子播放列表 URL。"""
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
async def fetch_huya_danmaku_params(room_id):
    try:
        resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}",
            headers={"User-Agent": MOBILE_UA, "Referer": "https://www.huya.com/"})
        html = resp.text
        ayyuid = int((re.search(r'"lYyid":(\d+)', html) or re.search(r'ayyuid:\s*["\']?(\d+)', html) or [None, 0])[1])
        top_sid = int((re.search(r'"lChannelId":(\d+)', html) or [None, 0])[1])
        sub_sid = int((re.search(r'"lSubChannelId":(\d+)', html) or [None, 0])[1])
        return {"platform": "huya", "ayyuid": ayyuid, "topSid": top_sid, "subSid": sub_sid}
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
                    logging.warning(f"[虎牙] {room_id} 网页抓取未匹配到昵称，m.huya.com 页面结构可能已变更")
            except Exception:
                logging.warning(f"[虎牙] {room_id} 网页抓取请求失败，无法补全昵称")
        anchor_name = anchor_name or "虎牙主播"

        avatar = (
            profile_info.get("avatar180") or profile_info.get("sAvatar180") or
            profile_info.get("sAvatar") or profile_info.get("avatar") or
            live_data.get("avatar180") or live_data.get("sAvatar180") or
            live_data.get("sAvatar") or live_data.get("avatar") or ""
        )

        if live.get("realLiveStatus") != "ON":
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        bitrate_list = live.get("liveBitRateInfo", [])

        quality_map = {}
        if bitrate_list:
            for item in bitrate_list:
                bitrate = item.get("bitrate", "")
                name = item.get("name", "") or f"{bitrate}K"
                quality_map[str(bitrate)] = name
        default_qualities = {
            "0": "原画",
            "4000": "蓝光",
            "2000": "超清",
            "1000": "高清",
            "500": "标清"
        }
        for k, v in default_qualities.items():
            if k not in quality_map:
                quality_map[k] = v

        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        if not cdn_list:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        cdn_list.sort(key=lambda s: CDN_ORDER.get(s.get("sCdnType", "ZZ"), 9))

        streams = []
        seen_urls = set()
        seen_qualities = set()

        for cdn in cdn_list:
            flv_url = cdn.get("sFlvUrl", "")
            base_stream_name = cdn.get("sStreamName", "")
            anti_code = cdn.get("sFlvAntiCode", "")
            suffix = cdn.get("sFlvUrlSuffix", "flv")
            cdn_type = cdn.get("sCdnType", "")
            cdn_label = CDN_NAMES.get(cdn_type, cdn_type or "CDN")

            if not (flv_url and base_stream_name and anti_code):
                continue

            for bitrate, quality_name in quality_map.items():
                if bitrate == "0":
                    stream_name = base_stream_name
                else:
                    stream_name = f"{base_stream_name}_{bitrate}"

                built = huya_build_anticode(anti_code, stream_name)
                full_url = f"{flv_url}/{stream_name}.{suffix}?{built}"
                full_url = full_url.replace("http://", "https://")

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                if quality_name in seen_qualities:
                    continue
                seen_qualities.add(quality_name)

                streams.append({
                    "cdn": f"{cdn_label}-{quality_name}",
                    "url": full_url,
                    "type": "flv"
                })

        if not streams:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        quality_order = {"原画": 0, "蓝光": 1, "超清": 2, "高清": 3, "标清": 4}
        def sort_key(s):
            q_name = s["cdn"].rsplit("-", 1)[-1]
            return quality_order.get(q_name, 99)
        streams.sort(key=sort_key)

        danmaku = await fetch_huya_danmaku_params(room_id)
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku, "isLive": True}
    except Exception as e:
        logging.exception("[虎牙] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 斗鱼 ====================
async def parse_douyu(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": f"https://www.douyu.com/{room_id}"}
        info_resp = await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)
        info = info_resp.json()
        room = info.get("room")
        if not room:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}

        name = "斗鱼主播"
        avatar = ""

        try:
            open_api = f"https://open.douyucdn.cn/api/RoomApi/room/{room_id}"
            open_resp = await request_with_retry("GET", open_api, headers={"User-Agent": UA})
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
        # 【修复】did 改为每次随机生成，避免所有用户共用同一 did 触发斗鱼风控
        did = hashlib.md5(f"douyu_{real_id}_{int(time.time() // 3600)}_{random.randint(0, 9999)}".encode()).hexdigest()[:32].ljust(32, "0")
        enc_resp = await request_with_retry("GET", f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}",
                                            headers={"User-Agent": UA})
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
            'hevc': '0',
            'fa': '0',
            'sov': '0',
            'enc_data': white['enc_data'],
            'tt': str(ts),
            'did': did,
            'auth': auth,
        }

        rate_map = {0: "原画", 2: "高清", 4: "标清"}

        async def fetch_rate(rate_val):
            params = base_params.copy()
            params['rate'] = str(rate_val)
            try:
                # 小 jitter 错开并发请求，避免同一毫秒同时打到斗鱼接口触发风控，
                # 但不再按画质梯度递增延迟（原 0.2*rate 导致标清慢 1s），整体解析更快
                await asyncio.sleep(random.uniform(0, 0.15))
                r = await request_with_retry("POST",
                    f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}",
                    headers=hdrs, data=params, timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    if d.get("error") == 0:
                        info = d["data"]
                        flv_url = f"{info['rtmp_url']}/{info['rtmp_live']}"
                        return rate_val, flv_url
                    else:
                        logging.warning(f"[斗鱼] 画质 rate={rate_val} 接口返回错误: {d.get('error')} {d.get('msg','')}")
                else:
                    logging.warning(f"[斗鱼] 画质 rate={rate_val} HTTP {r.status_code}")
            except Exception as e:
                logging.warning(f"[斗鱼] 画质 rate={rate_val} 请求异常: {e}")
            return rate_val, None

        tasks = [fetch_rate(r) for r in rate_map.keys()]
        results = await asyncio.gather(*tasks)

        streams = []
        seen_urls = set()
        for rate_val, flv_url in results:
            if flv_url and flv_url not in seen_urls:
                seen_urls.add(flv_url)
                label = rate_map.get(rate_val, f"画质{rate_val}")
                streams.append({"cdn": label, "url": flv_url, "type": "flv"})

        if not streams:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        quality_order = {"原画": 0, "高清": 1, "标清": 2}
        streams.sort(key=lambda s: quality_order.get(s["cdn"], 99))

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
        streams.sort(key=lambda x: 0 if x["type"] == "flv" else 1)
        if not streams:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        return {"streams": streams[:4], "title": name, "avatar": avatar, "isLive": True}
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
                logging.warning(f"[抖音] {url} 未获取到任何画质流，可能主播未开播，也可能 a_bogus 签名已失效")
                _DOUYIN_NO_STREAM_LOGGED[url] = time.time()
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}

        quality_order = {"原画": 0, "蓝光": 1, "超清": 2, "高清": 3, "标清": 4}
        streams.sort(key=lambda s: quality_order.get(s["cdn"].replace("抖音-", ""), 99))

        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit():
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA})
                match = re.search(r'"room_id":"(\d+)"', resp.text)
                if match:
                    room_id = match.group(1)
            except Exception:
                pass

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

        if not avatar:
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
                m_render = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', resp.text)
                if m_render:
                    render_json = json.loads(unquote(m_render.group(1)))
                    avatar = find_avatar_in_dict(render_json) or ""
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
                logging.warning(f"[Twitch] Client-ID {client_id} 头像查询返回空数据，尝试下一个...")
            except Exception:
                logging.warning(f"[Twitch] Client-ID {client_id} 头像查询异常，尝试下一个...")
                continue

        token, sig = None, None
        for client_id in TWITCH_CLIENT_IDS:
            gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
            if eff_cookie:
                gql_headers["Cookie"] = eff_cookie
            gql_url = "https://gql.twitch.tv/gql"
            payload = [{"operationName": "PlaybackAccessToken",
                         "variables": {"login": channel, "playerType": "embed"},
                         "query": "query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: \"web\", playerType: $playerType, playerBackend: \"mediaplayer\" }) { value signature } }"}]
            try:
                resp = await request_with_proxy_group("POST", gql_url, proxy_list=proxylist, json=payload,
                                                     headers=gql_headers, shuffle_proxy=False,
                                                     log_tag="Twitch-token")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        t = data[0].get("data", {}).get("streamPlaybackAccessToken")
                        if t:
                            token, sig = t.get("value"), t.get("signature")
                            if token and sig:
                                break
                logging.warning(f"[Twitch] Client-ID {client_id} 失效，尝试下一个...")
            except Exception:
                logging.warning(f"[Twitch] Client-ID {client_id} 请求异常")
        if not token or not sig:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}&allow_source=true&allow_audio_only=true&allow_spectre=true&fast_bread=true&allow_ads=false"
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist,
                                                     headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"},
                                                     shuffle_proxy=False, log_tag="Twitch-m3u8")
        if usher_resp.status_code != 200 or "#EXT-X-STREAM-INF" not in usher_resp.text:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        streams, lines = [], usher_resp.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                name = "source"
                if "RESOLUTION=" in line:
                    name = line.split("RESOLUTION=")[1].split(",")[0].replace("x", "p")
                if i+1 < len(lines):
                    sub_url = lines[i+1].strip()
                    if not sub_url.startswith("http"):
                        sub_url = urljoin(m3u8_url, sub_url)
                    streams.append({"cdn": f"Twitch-{name}", "url": sub_url, "type": "m3u8"})
        streams.sort(key=lambda s: (0 if "source" in s["cdn"] else 1, s["cdn"]))
        if not streams:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        return {"streams": streams, "title": nickname, "avatar": avatar, "channelName": channel, "isLive": True}
    except Exception as e:
        logging.exception("[Twitch] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== SOOP ====================
def _soop_offline_ttl(bj_id: str) -> int:
    """连续离线检测次数越多，TTL 越长：60s → 120 → 240 → 300s 封顶。"""
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
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result
        live_json = live_resp.json()
        channel = live_json.get('CHANNEL', {})
        nickname = channel.get('BJ_NM') or channel.get('BJNICK') or nickname
        result_code = channel.get('RESULT', -1)
        if result_code == -6:
            return {"streams": [], "isLive": False, "error": "19+成年直播间，请在设置中填入SOOP登录Cookie",
                    "title": nickname, "avatar": avatar}
        if result_code not in [0, 1]:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result

        broad_no = channel.get('BNO', '')
        if not broad_no:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result

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
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result
        cdn_json = cdn_resp.json()
        view_url = cdn_json.get('view_url')
        if not view_url:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result

        aid_form = live_data_form.copy()
        aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                  headers=headers_pc, data=aid_form, shuffle_proxy=False,
                                                  log_tag="SOOP-aid")
        if aid_resp.status_code != 200:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result
        aid_json = aid_resp.json()
        aid = aid_json.get('CHANNEL', {}).get('AID', '')
        if not aid:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + _soop_offline_ttl(bj_id)}
            return result

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

        result = {"streams": streams, "title": nickname, "avatar": avatar, "isLive": True}
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

    sem = asyncio.Semaphore(5)

    async def _one(item):
        url = item.get("url", "")
        cookie = item.get("cookie", "")
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
    """将 Go 服务探测时间戳清零，触发下次连接时重探。
    用 helper 封装是为了让嵌套异步函数能修改全局变量而无需在内层写 global 声明
    （Python 不允许 global 声明出现在赋值之后的同一作用域）。"""
    global _GO_SERVICE_AVAILABLE, _GO_SERVICE_LAST_CHECK
    _GO_SERVICE_LAST_CHECK = 0
    if mark_unavailable:
        _GO_SERVICE_AVAILABLE = False

@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if not await _check_go_service():
        try:
            await websocket.send_text(json.dumps({
                "method": "error",
                "content": "弹幕服务暂时不可用，请稍后重试"
            }))
            await websocket.close(code=1013)  # 1013 = Try Again Later
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
                    # 超时说明 Go 服务可能有问题，清零时间戳触发下次重探
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
        # Go 服务端口不可达（Connection refused 等），标记为不可用
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

@app.websocket("/ws/twitch/{channel_name}")
async def websocket_twitch_danmaku(ws_conn: WebSocket, channel_name: str):
    await ws_conn.accept()
    twitch_ws_url = "wss://irc-ws.chat.twitch.tv:443"
    try:
        async with websockets.connect(twitch_ws_url, ping_interval=30, ping_timeout=10) as twitch_ws:
            await twitch_ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await twitch_ws.send("PASS SCHMOOPIIE")
            await twitch_ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await twitch_ws.send(f"JOIN #{channel_name.lower()}")

            # 【修复3（Twitch WS泄漏）】参照抖音 WS 改用 create_task + FIRST_COMPLETED 模式，
            # 避免 forward_to_frontend 里 break 后 listen_to_frontend 永久挂起导致连接泄漏。
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
                                break  # 前端已断开
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
                                break  # 前端已断开
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
