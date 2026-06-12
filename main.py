import json
import re
import os
import httpx
import asyncio
import websockets
import time
import hashlib
import base64
import random
import logging
from fastapi import FastAPI, Query, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, Response, JSONResponse
from urllib.parse import unquote, urlparse, quote, urljoin
from contextlib import asynccontextmanager

# ==================== 日志配置 ====================
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

# ==================== 环境变量 ====================
PROXY_LIST_STR = os.getenv("PROXY_LIST", "").strip()
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()] if PROXY_LIST_STR else [None]
logging.info(f"[代理] 国内代理 {len(PROXY_URLS)} 个")

EXTERNAL_PROXY_LIST_STR = os.getenv("EXTERNAL_PROXY_LIST", "").strip()
EXTERNAL_PROXY_URLS = [p.strip() for p in EXTERNAL_PROXY_LIST_STR.split(",") if p.strip()] if EXTERNAL_PROXY_LIST_STR else [None]
logging.info(f"[代理] 外网代理 {len(EXTERNAL_PROXY_URLS)} 个")

CF_WORKER = os.getenv("CF_WORKER_URL", "").strip()
SOOP_COOKIE = os.getenv("SOOP_COOKIE", "").strip()
TWITCH_COOKIE = os.getenv("TWITCH_COOKIE", "").strip()
BILI_COOKIE = os.getenv("BILI_COOKIE", "").strip()

if CF_WORKER:
    logging.info(f"[CF Worker] 已配置: {CF_WORKER}")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36"

# ==================== 全局连接池 ====================
CLIENT_POOL: dict = {}
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15

async def get_client(proxy=None, timeout=None):
    if timeout is None: timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    if key in CLIENT_POOL:
        return CLIENT_POOL[key]
    async with CLIENT_LOCK:
        if key not in CLIENT_POOL:
            CLIENT_POOL[key] = httpx.AsyncClient(
                timeout=timeout,
                proxy=proxy,
                http2=(proxy is None),  # 【优化】代理连接关闭 HTTP/2 防兼容问题
                verify=False,
                follow_redirects=True,  # 【修复】强制跟随 302 重定向 (SOOP .co.kr 必需)
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
            )
    return CLIENT_POOL[key]

STREAM_PROXY_MAP: dict = {}
M3U8_CACHE: dict = {}

def get_fixed_proxy_list(proxy_pool):
    if not proxy_pool or proxy_pool[0] is None: return [None]
    primary = random.choice(proxy_pool)
    rest = [p for p in proxy_pool if p != primary]
    return [primary] + rest

async def request_with_retry(method, url, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for idx, proxy in enumerate(PROXY_URLS):
        try:
            client = await get_client(proxy, timeout)
            return await client.request(method, url, **kwargs)
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 6)  # 【优化】外网代理默认超时降至 6s，防卡死
    shuffle_proxy = kwargs.pop("shuffle_proxy", False)
    targets = proxy_list[:]
    if shuffle_proxy and targets: random.shuffle(targets)

    for idx, proxy in enumerate(targets):
        try:
            client = await get_client(proxy, timeout)
            return await client.request(method, url, **kwargs)
        except Exception as e:
            last_error = e
            await asyncio.sleep(0.5)
    raise last_error or Exception("所有代理均失败")

# ==================== 应用初始化 ====================
@asynccontextmanager
async def lifespan(app):
    async def _cache_cleanup():
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [k for k, v in list(M3U8_CACHE.items()) if v.get('expire', 0) < now]
            for k in expired: M3U8_CACHE.pop(k, None)
            if len(STREAM_PROXY_MAP) > 500:
                for k in list(STREAM_PROXY_MAP.keys())[:250]: STREAM_PROXY_MAP.pop(k, None)
    task = asyncio.create_task(_cache_cleanup())
    yield
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)

# 【新增】全局异常兜底，防止 Render 网关返回无 CORS 头的 500 HTML 导致前端跨域报错
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.exception(f"[全局异常] 未捕获的服务器错误: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"}
    )

# ==================== 代理接口 ====================
@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = [
        ".douyu.com", ".huya.com", ".bilibili.com", ".bilivideo.com", ".douyucdn.cn",
        ".douyin.com", ".live.bilibili.com", ".twitch.tv", ".ttvnw.net",
        ".sooplive.com", ".sooplive.net", ".sooplivecdn.com",
        ".pandalive.co.kr", ".live-video.net", ".pandalivecdn.com"
    ]
    
    def is_allowed_domain(u: str) -> bool:
        hostname = urlparse(u).hostname
        if not hostname: return False
        for domain in ALLOWED:
            clean = domain.lstrip('.')
            if hostname == clean or hostname.endswith('.' + clean): return True
        return False

    if not is_allowed_domain(url):
        raise HTTPException(403, "domain not allowed")

    body = await request.body() if request.method == "POST" else None
    headers = {"User-Agent": ua or UA, "Referer": referer or ""}
    if cookie: headers["Cookie"] = cookie
    if request.method == "POST": headers["Content-Type"] = "application/x-www-form-urlencoded"

    EXTERNAL_DOMAINS = ["twitch.tv", "ttvnw.net", "twitchsvc.net", "sooplive.com", "livestream-manager.sooplive.com", "pandalive.co.kr", "live-video.net", "pandalivecdn.com"]
    use_external = any(d in url for d in EXTERNAL_DOMAINS) or any(d in (referer or "") for d in ["twitch.tv", "player.twitch.tv", "sooplive.com", "pandalive.co.kr"])
    proxy_list = EXTERNAL_PROXY_URLS if use_external else PROXY_URLS

    is_ts = url.lower().split("?")[0].endswith(".ts")
    if is_ts:
        stream_key = hashlib.md5(url.encode()).hexdigest()[:16]
        if stream_key not in STREAM_PROXY_MAP:
            STREAM_PROXY_MAP[stream_key] = random.choice(proxy_list) if proxy_list and proxy_list[0] else None
        fixed_proxy = STREAM_PROXY_MAP[stream_key]
        proxies_to_use = [fixed_proxy] if fixed_proxy else proxy_list
        shuffle_proxy = False
    else:
        proxies_to_use = proxy_list
        shuffle_proxy = True

    resp = await request_with_proxy_group(request.method, url, proxy_list=proxies_to_use, headers=headers, content=body, shuffle_proxy=shuffle_proxy)
    content_type = resp.headers.get("content-type", "")
    is_m3u8 = "mpegurl" in content_type.lower() or url.split("?")[0].endswith(".m3u8")

    if is_m3u8:
        base_url = url.rsplit("/", 1)[0] + "/"
        parsed_cdn = urlparse(url)
        cdn_origin = f"{parsed_cdn.scheme}://{parsed_cdn.netloc}"
        proxy_base = str(request.base_url).rstrip("/") + "/api/proxy"
        _is_foreign = any(d in url for d in ("live-video.net", "pandalive", "sooplive", "ttvnw", "twitch"))
        if CF_WORKER and _is_foreign: proxy_base = CF_WORKER.rstrip("/")
        
        eff_referer = referer or ("https://www.pandalive.co.kr/" if "pandalive" in url or "live-video.net" in url else "https://player.twitch.tv" if "twitch" in url or "ttvnw" in url else "https://play.sooplive.com")
        lines = resp.text.splitlines()
        rewritten = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                abs_url = stripped if stripped.startswith("http") else (cdn_origin + stripped if stripped.startswith("/") else base_url + stripped)
                rewritten.append(f"{proxy_base}?url={quote(abs_url, safe='')}&referer={quote(eff_referer, safe='')}")
            else:
                rewritten.append(line)
        body_out = "\n".join(rewritten).encode("utf-8")
        # 【修复】直接返回 Response，消灭 gen.throw
        return Response(content=body_out, status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/vnd.apple.mpegurl"})

    if is_ts:
        # 【修复】安全生成器，吞噬客户端断开导致的 CancelledError
        async def safe_aiter_bytes(response):
            try:
                async for chunk in response.aiter_bytes(): yield chunk
            except (asyncio.CancelledError, GeneratorExit, Exception): pass
            finally: await response.aclose()
            
        return StreamingResponse(safe_aiter_bytes(resp), status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "video/mp2t"})

    return Response(content=resp.content, status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "application/json"})

# ==================== 平台解析 ====================
def huya_build_anticode(raw_anti, stream_name):
    anti = raw_anti.replace("&", "&")
    params = dict(p.split("=", 1) for p in anti.split("&") if "=" in p)
    fm = params.get("fm", "")
    ws_time = params.get("wsTime", "")
    if not fm or not ws_time: return anti
    try: fm_dec = base64.b64decode(fm.replace("%2B", "+").replace("%2F", "/").replace("%3D", "=") + "==").decode()
    except Exception:
        try: fm_dec = base64.b64decode(unquote(fm) + "==").decode()
        except Exception: return anti
    p = fm_dec.split(" ")[0]
    seqid = str(int(time.time() * 10000 + random.random() * 10000))
    ws_secret = hashlib.md5(f"{p}0{stream_name}{seqid}_{ws_time}".encode()).hexdigest()
    params["wsSecret"] = ws_secret
    params["seqid"] = seqid
    params["u"] = "0"
    return "&".join(f"{k}={v}" for k, v in params.items())

async def parse_huya(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        resp = await request_with_retry("GET", f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}", headers={"User-Agent": UA, "Referer": "https://www.huya.com/"})
        data = resp.json()
        if data.get("status") != 200: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        live = data["data"]
        profile_info = live.get("profileInfo", {})
        live_data = live.get("liveData", {})
        anchor_name = profile_info.get("nick") or live_data.get("nick") or "虎牙主播"
        avatar = profile_info.get("avatar180") or live_data.get("avatar180") or ""
        if live.get("realLiveStatus") != "ON": return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        
        CDN_NAMES = {"AL": "阿里云", "TX": "腾讯云", "HW": "华为云", "WS": "网宿", "BD": "百度云"}
        quality_map = {"0": "原画", "4000": "蓝光", "2000": "超清", "1000": "高清", "500": "标清"}
        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        streams, seen_urls = [], set()
        for cdn in cdn_list:
            flv_url, base_stream_name, anti_code = cdn.get("sFlvUrl"), cdn.get("sStreamName"), cdn.get("sFlvAntiCode")
            if not (flv_url and base_stream_name and anti_code): continue
            for bitrate, q_name in quality_map.items():
                stream_name = base_stream_name if bitrate == "0" else f"{base_stream_name}_{bitrate}"
                full_url = f"{flv_url}/{stream_name}.flv?{huya_build_anticode(anti_code, stream_name)}".replace("http://", "https://")
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    streams.append({"cdn": f"{CDN_NAMES.get(cdn.get('sCdnType','CDN'))}-{q_name}", "url": full_url, "type": "flv"})
        streams.sort(key=lambda s: {"原画":0,"蓝光":1,"超清":2,"高清":3,"标清":4}.get(s["cdn"].split("-")[-1], 99))
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "isLive": True}
    except Exception as e:
        logging.exception("[虎牙] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

async def parse_douyu(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": f"https://www.douyu.com/{room_id}"}
        name, avatar = "斗鱼主播", ""
        try:
            open_resp = await request_with_retry("GET", f"https://open.douyucdn.cn/api/RoomApi/room/{room_id}", headers={"User-Agent": UA})
            if open_resp.status_code == 200:
                d = open_resp.json().get("data", {})
                name = d.get("owner_name") or name
                avatar = d.get("avatar_big") or d.get("avatar") or ""
        except: pass
        if not avatar:
            info_resp = await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)
            room = info_resp.json().get("room", {})
            name = room.get("nickname") or name
            avatar = room.get("owner", {}).get("avatar", {}).get("big") or room.get("room_pic") or ""
        if avatar and avatar.startswith("//"): avatar = "https:" + avatar
        if room.get("show_status") != 1: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        
        real_id = str(room["room_id"])
        did = "10000000000000000000000000001501"
        enc_resp = await request_with_retry("GET", f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}", headers={"User-Agent": UA})
        white = enc_resp.json()["data"]
        ts = int(time.time())
        secret = white['rand_str']
        for _ in range(white['enc_time']): secret = hashlib.md5((secret + white['key']).encode()).hexdigest()
        auth = hashlib.md5((secret + white['key'] + f"{real_id}{ts}").encode()).hexdigest()
        base_params = {'ver': '219032101', 'rid': real_id, 'enc_data': white['enc_data'], 'tt': str(ts), 'did': did, 'auth': auth}
        rate_map = {0: "原画", 2: "高清", 4: "标清"}
        
        async def fetch_rate(rate_val):
            params = base_params.copy(); params['rate'] = str(rate_val)
            await asyncio.sleep(0.2 * rate_val)
            try:
                r = await request_with_retry("POST", f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}", headers=hdrs, data=params, timeout=10)
                if r.status_code == 200 and r.json().get("error") == 0:
                    info = r.json()["data"]
                    return rate_val, f"{info['rtmp_url']}/{info['rtmp_live']}"
            except: pass
            return rate_val, None
            
        results = await asyncio.gather(*[fetch_rate(r) for r in rate_map.keys()])
        streams = [{"cdn": rate_map.get(r, "画质"), "url": u, "type": "flv"} for r, u in results if u]
        streams.sort(key=lambda s: {"原画":0,"高清":1,"标清":2}.get(s["cdn"], 99))
        return {"streams": streams, "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        logging.exception("[斗鱼] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

async def parse_bilibili(url):
    try:
        rid = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
        if BILI_COOKIE: hdrs["Cookie"] = BILI_COOKIE
        room_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers=hdrs)
        room_data = room_resp.json()
        if room_data.get("code") != 0: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        real_rid = room_data["data"]["room_id"]
        
        name, avatar = "B站主播", ""
        try:
            # 【修复】使用 get_anchor_in_room 防 -352 风控
            anchor_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid={rid}", headers=hdrs)
            info = anchor_resp.json().get("data", {}).get("info", {})
            name = info.get("uname") or name
            avatar = info.get("face") or ""
        except: pass
        
        if room_data["data"].get("live_status") != 1: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        play_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8", headers=hdrs)
        play = play_resp.json()
        if play.get("code") != 0: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        
        streams, seen = [], set()
        for stream in play["data"].get("playurl_info", {}).get("playurl", {}).get("stream", []):
            for fmt in stream.get("format", []):
                for codec in fmt.get("codec", []):
                    for info in codec.get("url_info", []):
                        u = info["host"] + codec["base_url"] + info["extra"]
                        if u not in seen:
                            seen.add(u)
                            streams.append({"cdn": f"{fmt['format_name'].upper()}", "url": u, "type": "flv" if fmt["format_name"] == "flv" else "m3u8"})
        return {"streams": streams[:4], "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        logging.exception("[B站] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

async def parse_douyin(url):
    try:
        if not DouyinLiveStream: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        live = DouyinLiveStream()
        data = await live.fetch_web_stream_data(url, process_data=True)
        qualities = ["OD", "UHD", "HD", "SD", "LD"]
        quality_names = {"OD": "原画", "UHD": "蓝光", "HD": "超清", "SD": "高清", "LD": "标清"}
        
        async def _fetch_quality(q):
            try:
                stream_obj = await live.fetch_stream_url(data, q)
                raw = json.loads(stream_obj.to_json())
                return q, raw.get("flv_url", ""), raw.get("m3u8_url", ""), raw.get("anchor_name", "")
            except: return q, "", "", ""
            
        results = await asyncio.gather(*[_fetch_quality(q) for q in qualities])
        streams, seen_urls, anchor_name = [], set(), "抖音主播"
        for q, flv, m3u8, name in results:
            if name and anchor_name == "抖音主播": anchor_name = name
            label = quality_names.get(q, q)
            if flv and flv not in seen_urls:
                seen_urls.add(flv)
                streams.append({"cdn": f"抖音-{label}", "url": flv, "type": "flv"})
            elif m3u8 and m3u8 not in seen_urls:
                seen_urls.add(m3u8)
                streams.append({"cdn": f"抖音-{label}", "url": m3u8, "type": "m3u8"})
                
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit():
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": "Mozilla/5.0"})
                match = re.search(r'"room_id":"(\d+)"', resp.text)
                if match: room_id = match.group(1)
            except: pass
            
        # 【修复】深度递归提取 streamget 数据中的头像
        def find_avatar_in_dict(d, depth=0):
            if depth > 12: return None
            if isinstance(d, dict):
                for k, v in d.items():
                    if k in ("avatarThumb", "avatar_thumb", "avatar_larger", "avatar") and isinstance(v, dict):
                        urls = v.get("urlList") or v.get("url_list") or v.get("url")
                        if isinstance(urls, list) and urls: return urls[0]
                        elif isinstance(urls, str) and urls.startswith("http"): return urls
                    res = find_avatar_in_dict(v, depth + 1)
                    if res: return res
            elif isinstance(d, list):
                for item in d:
                    res = find_avatar_in_dict(item, depth + 1)
                    if res: return res
            return None
            
        avatar = find_avatar_in_dict(data) or ""
        if not avatar:
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
                m_render = re.search(r'<script id="RENDER_DATA" type="application/json">([^<]+)</script>', resp.text)
                if m_render:
                    render_json = json.loads(unquote(m_render.group(1)))
                    avatar = find_avatar_in_dict(render_json) or ""
            except: pass
        if avatar and avatar.startswith("//"): avatar = "https:" + avatar
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "roomId": room_id, "isLive": True}
    except Exception as e:
        logging.exception("[抖音] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

TWITCH_CLIENT_IDS = ["kimne78kx3ncx6brgo4mv6wki5h1ko", "ue666xxq81dq0l30715w03p3h3a6h", "8557m2777l2623943p9951150w621"]
async def parse_twitch(url, cookie: str = ""):
    try:
        match = re.search(r"twitch.tv/([^/?]+)", url)
        if not match: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        channel = match.group(1)
        eff_cookie = cookie or TWITCH_COOKIE
        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        
        nickname, avatar = channel, ""
        for client_id in TWITCH_CLIENT_IDS:
            try:
                gql_avatar_payload = [{"operationName": "UserAvatar", "variables": {"login": channel}, "query": "query UserAvatar($login: String!) { user(login: $login) { profileImageURL(width: 300) displayName } }"}]
                gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
                if eff_cookie: gql_headers["Cookie"] = eff_cookie
                avatar_resp = await request_with_proxy_group("POST", "https://gql.twitch.tv/gql", proxy_list=proxylist, json=gql_avatar_payload, headers=gql_headers, shuffle_proxy=False)
                if avatar_resp.status_code == 200:
                    av_data = avatar_resp.json()
                    if isinstance(av_data, list) and av_data[0].get("data", {}).get("user"):
                        user_info = av_data[0]["data"]["user"]
                        avatar = user_info.get("profileImageURL", "")
                        nickname = user_info.get("displayName", channel)
                        break
            except: continue

        token, sig = None, None
        for client_id in TWITCH_CLIENT_IDS:
            gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
            if eff_cookie: gql_headers["Cookie"] = eff_cookie
            payload = [{"operationName": "PlaybackAccessToken", "variables": {"login": channel, "playerType": "embed"}, "query": 'query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: "web", playerType: $playerType, playerBackend: "mediaplayer" }) { value signature } }'}]
            try:
                resp = await request_with_proxy_group("POST", "https://gql.twitch.tv/gql", proxy_list=proxylist, json=payload, headers=gql_headers, shuffle_proxy=False)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        t = data[0].get("data", {}).get("streamPlaybackAccessToken")
                        if t:
                            token, sig = t.get("value"), t.get("signature")
                            if token and sig: break
            except: continue
            
        if not token or not sig: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        
        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}&allow_source=true&allow_audio_only=true&allow_ads=false"
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist, headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"}, shuffle_proxy=False)
        if usher_resp.status_code != 200: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        
        streams, lines = [], usher_resp.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                name = "source"
                if "RESOLUTION=" in line: name = line.split("RESOLUTION=")[1].split(",")[0].replace("x", "p")
                if i+1 < len(lines):
                    sub_url = lines[i+1].strip()
                    if not sub_url.startswith("http"): sub_url = urljoin(m3u8_url, sub_url)
                    streams.append({"cdn": f"Twitch-{name}", "url": sub_url, "type": "m3u8"})
        return {"streams": streams, "title": nickname, "avatar": avatar, "channelName": channel, "isLive": True}
    except Exception as e:
        logging.exception("[Twitch] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

async def parse_soop(url, cookie: str = ""):
    try:
        eff_cookie = cookie or SOOP_COOKIE
        cache_key = url + ("_auth" if eff_cookie else "")
        cached = M3U8_CACHE.get(cache_key)
        if cached and cached["expire"] > time.time(): return cached["data"]
        
        bj_id = url.rstrip('/').split('/')[3].split('?')[0] if len(url.split('/')) > 3 else url.split('/')[-1].split('?')[0]
        headers_pc = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0', 'content-type': 'application/x-www-form-urlencoded; charset=UTF-8', 'origin': 'https://play.sooplive.com', 'referer': 'https://play.sooplive.com'}
        if eff_cookie: headers_pc['cookie'] = eff_cookie
        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        
        nickname, avatar = f'BJ-{bj_id}', ''
        try:
            info_apis = [f'https://st.sooplive.co.kr/api/get_station_info.php?szBjId={bj_id}', f'https://st.sooplive.com/api/get_station_info.php?szBjId={bj_id}']
            for info_api in info_apis:
                info_resp = await request_with_proxy_group("GET", info_api, proxy_list=proxylist, headers=headers_pc, shuffle_proxy=False, timeout=3)
                if info_resp.status_code == 200:
                    si = info_resp.json().get('station', {})
                    nickname = si.get('user_nick') or si.get('bj_nick') or nickname
                    avatar = si.get('profile_image') or si.get('profile_img') or ''
                    if avatar:
                        if avatar.startswith('//'): avatar = 'https:' + avatar
                        break
        except: pass
        
        if not avatar:
            try:
                home_resp = await request_with_proxy_group("GET", f'https://play.sooplive.com/{bj_id}', proxy_list=proxylist, headers=headers_pc, shuffle_proxy=False, timeout=5)
                if home_resp.status_code == 200:
                    m = re.search(r'<meta\s+(?:property|name)="og:image"\s+content="([^"]+)"', home_resp.text) or re.search(r'"profile_image"\s*:\s*"([^"]+)"', home_resp.text)
                    if m:
                        avatar = m.group(1).replace('\\u002F', '/').replace('\\/', '/')
                        if avatar.startswith('//'): avatar = 'https:' + avatar
            except: pass

        live_api = f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}'
        live_data_form = {'bid': bj_id, 'bno': '', 'type': '', 'pwd': '', 'player_type': 'html5', 'stream_type': 'common', 'quality': 'master', 'mode': 'landing', 'from_api': '0', 'is_revive': 'false'}
        live_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist, headers=headers_pc, data=live_data_form, shuffle_proxy=False)
        if live_resp.status_code != 200: 
            res = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 30}; return res
            
        channel = live_resp.json().get('CHANNEL', {})
        if channel.get('RESULT') == -6: return {"streams": [], "isLive": False, "error": "19+成年直播间，请在设置中填入SOOP登录Cookie", "title": nickname, "avatar": avatar}
        if channel.get('RESULT') not in [0, 1]: 
            res = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 30}; return res
            
        broad_no = channel.get('BNO', '')
        if not broad_no: 
            res = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 30}; return res
            
        cdn_params = {'return_type': 'gcp_cdn', 'use_cors': 'false', 'cors_origin_url': 'play.sooplive.com', 'broad_key': f'{broad_no}-common-master-hls', 'time': str(time.time())}
        cdn_resp = await request_with_proxy_group("GET", 'http://livestream-manager.sooplive.com/broad_stream_assign.html', proxy_list=proxylist, headers=headers_pc, params=cdn_params, shuffle_proxy=False)
        view_url = cdn_resp.json().get('view_url') if cdn_resp.status_code == 200 else None
        if not view_url: 
            res = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 30}; return res
            
        aid_form = live_data_form.copy(); aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist, headers=headers_pc, data=aid_form, shuffle_proxy=False)
        aid = aid_resp.json().get('CHANNEL', {}).get('AID', '') if aid_resp.status_code == 200 else ''
        if not aid: 
            res = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 30}; return res
            
        m3u8_url = f'{view_url}?aid={aid}'
        streams = []
        try:
            master_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://play.sooplive.com"}, shuffle_proxy=False)
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        name = "Source"
                        res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                        if res_match: name = res_match.group(1).split('x')[1] + 'p'
                        if i + 1 < len(lines):
                            sub_url = lines[i + 1].strip()
                            if sub_url:
                                if not sub_url.startswith("http"): sub_url = urljoin(m3u8_url, sub_url)
                                streams.append({"cdn": f"SOOP-{name}", "url": sub_url, "type": "m3u8"})
        except: pass
        if not streams: streams = [{"cdn": "SOOP-Source", "url": m3u8_url, "type": "m3u8"}]
        res = {"streams": streams, "title": nickname, "avatar": avatar, "isLive": True}
        M3U8_CACHE[cache_key] = {"data": res, "expire": time.time() + 15}
        return res
    except Exception as e:
        logging.exception("[SOOP] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

async def parse_panda_manual(url):
    try:
        cached = M3U8_CACHE.get(url)
        if cached and cached["expire"] > time.time(): return cached["data"]
        user_id = url.split('?')[0].rstrip('/').split('/')[-1]
        headers = {'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/', 'user-agent': 'Mozilla/5.0'}
        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        
        resp = await request_with_proxy_group("POST", 'https://api.pandalive.co.kr/v1/member/bj', proxy_list=proxylist, headers=headers, data={'userId': user_id, 'info': 'media fanGrade'}, shuffle_proxy=False)
        if resp.status_code != 200 or 'bjInfo' not in resp.json(): return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        bj_info = resp.json()['bjInfo']
        anchor_name = bj_info.get('nick', user_id)
        
        _IMG_FIELDS = ('thumbUrl', 'profileImg', 'profileImage', 'img', 'userImg', 'thumbImg', 'bjImg', 'thumb', 'photo', 'avatar', 'iconImg', 'userPic', 'thumbnail')
        avatar = next((bj_info[k] for k in _IMG_FIELDS if bj_info.get(k)), '')
        
        resp2 = await request_with_proxy_group("POST", 'https://api.pandalive.co.kr/v1/live/play', proxy_list=proxylist, headers=headers, data={'action': 'watch', 'userId': user_id, 'password': '', 'shareLinkType': ''}, shuffle_proxy=False)
        if resp2.status_code != 200 or 'PlayList' not in resp2.json() or 'hls' not in resp2.json()['PlayList']: 
            res = {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
            M3U8_CACHE[url] = {"data": res, "expire": time.time() + 30}; return res
            
        play_json = resp2.json()
        if not avatar or 'default' in avatar.lower():
            for _section in ('bjInfo', 'userInfo', 'bjProfile', 'channelInfo'):
                _d = play_json.get(_section)
                if isinstance(_d, dict):
                    _candidate = next((str(_d[k]) for k in _IMG_FIELDS if _d.get(k)), '')
                    if _candidate: avatar = _candidate; break
                    
        if avatar and avatar.startswith('//'): avatar = 'https:' + avatar
        elif avatar and not avatar.startswith('http'): avatar = 'https://profile.pandalive.co.kr/' + avatar.lstrip('/')
        
        real_m3u8 = play_json['PlayList']['hls'][0]['url']
        streams = []
        try:
            fetch_url = f"{CF_WORKER.rstrip('/')}?url={quote(real_m3u8, safe='')}&referer={quote('https://www.pandalive.co.kr/', safe='')}" if CF_WORKER else real_m3u8
            master_resp = await request_with_proxy_group("GET", fetch_url, proxy_list=[None] if CF_WORKER else proxylist, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.pandalive.co.kr/"}, shuffle_proxy=False)
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        name = "Source"
                        res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                        if res_match: name = res_match.group(1).split('x')[1] + 'p'
                        if i + 1 < len(lines):
                            sub_url = lines[i + 1].strip()
                            if sub_url:
                                if not sub_url.startswith("http"): sub_url = urljoin(real_m3u8, sub_url)
                                streams.append({"cdn": f"PandaTV-{name}", "url": sub_url, "type": "m3u8"})
        except: pass
        if not streams: streams = [{"cdn": "PandaTV-Source", "url": real_m3u8, "type": "m3u8"}]
        res = {"streams": streams, "title": anchor_name, "avatar": avatar, "isLive": True}
        M3U8_CACHE[url] = {"data": res, "expire": time.time() + 15}
        return res
    except Exception as e:
        logging.exception("[PandaTV] 解析异常")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 轻量级状态检测接口（专为关注列表设计） ====================
@app.get("/api/status")
async def api_status(url: str = Query(...)):
    """仅检测直播间是否在线，不解析流地址，极大降低后端 CPU 和网络开销。"""
    try:
        is_live = False
        if "huya.com" in url:
            room_id = url.rstrip("/").split("/")[-1].split("?")[0]
            resp = await request_with_retry("GET", f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}", headers={"User-Agent": UA}, timeout=5)
            is_live = resp.json().get("data", {}).get("realLiveStatus") == "ON"
        elif "douyu.com" in url:
            room_id = url.rstrip("/").split("/")[-1].split("?")[0]
            resp = await request_with_retry("GET", f"https://open.douyucdn.cn/api/RoomApi/room/{room_id}", headers={"User-Agent": UA}, timeout=5)
            is_live = resp.json().get("data", {}).get("room_status") == "1"
        elif "bilibili.com" in url:
            rid = url.rstrip("/").split("/")[-1].split("?")[0]
            resp = await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/"}, timeout=5)
            is_live = resp.json().get("data", {}).get("live_status") == 1
        elif "sooplive.com" in url:
            bj_id = url.rstrip('/').split('/')[-1].split('?')[0]
            resp = await request_with_proxy_group("POST", f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}', 
                                                  proxy_list=get_fixed_proxy_list(EXTERNAL_PROXY_URLS),
                                                  headers={'origin': 'https://play.sooplive.com', 'referer': 'https://play.sooplive.com'}, 
                                                  data={'bid': bj_id, 'bno': '', 'type': '', 'pwd': '', 'player_type': 'html5', 'stream_type': 'common', 'quality': 'master', 'mode': 'landing', 'from_api': '0', 'is_revive': 'false'}, 
                                                  shuffle_proxy=False, timeout=5)
            if resp.status_code == 200:
                is_live = resp.json().get('CHANNEL', {}).get('RESULT') in [0, 1]
        elif "pandalive.co.kr" in url:
            user_id = url.split('?')[0].rstrip('/').split('/')[-1]
            resp = await request_with_proxy_group("POST", 'https://api.pandalive.co.kr/v1/member/bj', 
                                                  proxy_list=get_fixed_proxy_list(EXTERNAL_PROXY_URLS),
                                                  headers={'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/'}, 
                                                  data={'userId': user_id, 'info': 'media fanGrade'}, shuffle_proxy=False, timeout=5)
            if resp.status_code == 200:
                is_live = 'media' in resp.json()
        return {"isLive": is_live}
    except Exception as e:
        logging.warning(f"[状态检测] {url} 异常: {e}")
        return {"isLive": False}

@app.get("/api/parse")
async def api_parse(url: str = Query(...), cookie: str = Query("")):
    try:
        if "huya.com" in url: return await parse_huya(url)
        if "douyu.com" in url: return await parse_douyu(url)
        if "bilibili.com" in url: return await parse_bilibili(url)
        if "douyin.com" in url: return await parse_douyin(url)
        if "twitch.tv" in url: return await parse_twitch(url, cookie=cookie)
        if "sooplive.com" in url: return await parse_soop(url, cookie=cookie)
        if "pandalive.co.kr" in url: return await parse_panda_manual(url)
        raise HTTPException(400, "不支持的平台")
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

# ==================== 弹幕代理 ====================
@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    await websocket.accept()
    try:
        async with websockets.connect(f"ws://localhost:1088/ws/{room_id}", ping_interval=None) as go_ws:
            async def forward_to_go():
                while True:
                    data = await websocket.receive_text()
                    if data == 'ping':
                        try: await go_ws.ping()
                        except: pass
                        await websocket.send_text('pong')
                    elif go_ws.open: await go_ws.send(data)
            async def forward_to_frontend():
                while True:
                    data = await go_ws.recv()
                    if isinstance(data, bytes): data = data.decode("utf-8")
                    await websocket.send_text(data)
            await asyncio.gather(forward_to_go(), forward_to_frontend(), return_exceptions=True)
    except Exception as e:
        logging.warning(f"[WS] 抖音代理异常: {e}")
    finally:
        try: await websocket.close()
        except: pass

# 【修复】Twitch 弹幕改用原生异步 websockets，彻底消灭线程泄漏
@app.websocket("/ws/twitch/{channel_name}")
async def websocket_twitch_danmaku(ws_conn: WebSocket, channel_name: str):
    await ws_conn.accept()
    try:
        async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=30, ping_timeout=10) as twitch_ws:
            await twitch_ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await twitch_ws.send("PASS SCHMOOPIIE")
            await twitch_ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await twitch_ws.send(f"JOIN #{channel_name.lower()}")
            async def forward_to_frontend():
                async for msg in twitch_ws:
                    if msg.startswith("PING"):
                        await twitch_ws.send("PONG :tmi.twitch.tv")
                        continue
                    m = re.match(r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)", msg)
                    if m: await ws_conn.send_json({"type": "chat", "nick": m.group(1), "content": m.group(2)})
            async def heartbeat():
                while True:
                    await asyncio.sleep(15)
                    try:
                        data = await asyncio.wait_for(ws_conn.receive_text(), timeout=1)
                        if data == "ping": await ws_conn.send_text("pong")
                    except asyncio.TimeoutError: pass
                    except Exception: break
            await asyncio.gather(forward_to_frontend(), heartbeat())
    except Exception as e:
        logging.warning(f"[Twitch WS] 连接异常: {e}")
    finally:
        try: await ws_conn.close()
        except: pass

@app.get("/")
def root(): return {"status":"ok"}

@app.api_route("/health", methods=["GET","HEAD"])
async def health(): return {"status":"alive"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
