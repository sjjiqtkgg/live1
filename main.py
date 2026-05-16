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
    # 启动时开始后台任务：每60秒清理过期缓存
    async def _cache_cleanup():
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [k for k, v in list(M3U8_CACHE.items()) if v.get('expire', 0) < now]
            for k in expired:
                M3U8_CACHE.pop(k, None)
            if expired:
                print(f"[缓存] 清理 {len(expired)} 条过期条目，剩余 {len(M3U8_CACHE)} 条")
            # STREAM_PROXY_MAP 超过500条时清半（保留最新的250条）
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

# ==================== 代理配置 ====================
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

# ==================== 全局连接池 ====================
CLIENT_POOL: dict = {}
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15

async def get_client(proxy=None, timeout=None):
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    async with CLIENT_LOCK:
        if key not in CLIENT_POOL:
            CLIENT_POOL[key] = httpx.AsyncClient(
                timeout=timeout,
                proxy=proxy,
                http2=True,
                verify=False,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20
                )
            )
        return CLIENT_POOL[key]

# ==================== 流媒体代理映射（TS 按主 m3u8 URL 固定） ====================
STREAM_PROXY_MAP: dict = {}

# ==================== m3u8 缓存 ====================
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
    raise last_error or Exception("所有代理均失败")

# ------------------ 代理接口不变 -----------------
@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = [
        ".douyu.com", ".huya.com", ".bilibili.com", ".bilivideo.com", ".douyucdn.cn",
        ".douyin.com", ".live.bilibili.com", ".twitch.tv", ".ttvnw.net",
        ".sooplive.com", ".sooplive.net", ".sooplivecdn.com",
        ".pandalive.co.kr",
        ".live-video.net",
        ".pandalivecdn.com",
    ]
    if not any(urlparse(url).hostname.endswith(domain) for domain in ALLOWED):
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
    is_ts = url.lower().endswith(".ts")

    if is_ts:
        stream_key = referer or url
        if stream_key not in STREAM_PROXY_MAP:
            if proxy_list and proxy_list[0] is not None:
                STREAM_PROXY_MAP[stream_key] = random.choice(proxy_list)
            else:
                STREAM_PROXY_MAP[stream_key] = None
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
        return StreamingResponse(iter([body_out]), status_code=resp.status_code, headers=out_headers)

    if is_ts:
        return StreamingResponse(
            resp.aiter_bytes(),
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

        # 提取主播信息（不论是否开播）
        profile = live.get("profileRoom", {})
        room_info = live.get("roomInfo", {})
        live_data = live.get("liveData", {})
        anchor = live.get("anchor", {})
        anchor_name = (
            profile.get("nick") or room_info.get("nick") or live_data.get("nick") or anchor.get("nick") or
            profile.get("sNick") or room_info.get("sNick") or ""
        )
        if not anchor_name:
            try:
                mob_resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}",
                    headers={"User-Agent": MOBILE_UA, "Referer": "https://www.huya.com/"})
                mob_html = mob_resp.text
                m = re.search(r'"nick":"([^"]+)"', mob_html) or re.search(r'<title>([^_<]+)', mob_html)
                if m:
                    anchor_name = m.group(1).strip()
            except Exception:
                pass
        anchor_name = anchor_name or "虎牙主播"
        avatar = (
            profile.get("sAvatar180") or profile.get("sAvatar") or
            profile.get("avatar") or anchor.get("sAvatar180") or
            anchor.get("sAvatar") or anchor.get("avatar") or
            room_info.get("sAvatar180") or room_info.get("sAvatar") or ""
        )

        if live.get("realLiveStatus") != "ON":
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        if not cdn_list:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        cdn_list.sort(key=lambda s: CDN_ORDER.get(s.get("sCdnType", "ZZ"), 9))
        streams, seen = [], set()
        for s in cdn_list:
            cdn_type = s.get("sCdnType", "")
            if cdn_type in seen: continue
            flv_url = s.get("sFlvUrl", "")
            stream_name = s.get("sStreamName", "")
            anti_code = s.get("sFlvAntiCode", "")
            suffix = s.get("sFlvUrlSuffix", "flv")
            if not (flv_url and stream_name and anti_code): continue
            built = huya_build_anticode(anti_code, stream_name)
            full_url = f"{flv_url}/{stream_name}.{suffix}?{built}"
            label = CDN_NAMES.get(cdn_type, cdn_type or "CDN")
            streams.append({"cdn": label, "url": full_url.replace("http://", "https://"), "type": "flv"})
            seen.add(cdn_type)
        if not streams:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        danmaku = await fetch_huya_danmaku_params(room_id)
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku, "isLive": True}
    except Exception as e:
        print(f"[虎牙] 解析异常: {e}")
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
        name = room.get("nickname") or "斗鱼主播"
        avatar = room.get("room_icon", "")
        if isinstance(avatar, dict):
            avatar = avatar.get("big") or avatar.get("middle") or ""

        if room.get("show_status") != 1 or room.get("videoLoop") == 1:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        real_id = str(room["room_id"])
        did = "10000000000000000000000000001501"
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
        params = {
            'rate': '0',
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
        stream_resp = await request_with_retry("POST", f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}",
                                               headers=hdrs, data=params)
        stream_data = stream_resp.json()
        if stream_data.get("error") != 0:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        info_stream = stream_data["data"]
        flv_url = f"{info_stream['rtmp_url']}/{info_stream['rtmp_live']}" if info_stream.get('rtmp_url') and info_stream.get('rtmp_live') else None
        hls_url = info_stream.get('hls_url')
        streams = []
        if flv_url:
            streams.append({"cdn": "FLV", "url": flv_url, "type": "flv"})
        if hls_url and hls_url.startswith("http"):
            streams.append({"cdn": "HLS", "url": hls_url, "type": "m3u8"})
        if not streams:
            return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        return {"streams": streams, "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[斗鱼] 解析异常: {e}")
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

        # 提前获取主播信息
        name, avatar = "B站主播", ""
        try:
            ir_resp = await request_with_retry("GET",
                f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}",
                headers=hdrs)
            ir = ir_resp.json()
            ri = ir.get("data", {}).get("room_info", {})
            name = ri.get("uname") or name
            avatar = ri.get("face") or ""
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
        print(f"[B站] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 抖音 ====================
async def parse_douyin(url):
    try:
        from streamget import DouyinLiveStream
        live = DouyinLiveStream()
        data = await live.fetch_web_stream_data(url, process_data=True)
        stream_obj = await live.fetch_stream_url(data, "OD")
        raw = json.loads(stream_obj.to_json())
        streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
        if not streams:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit():
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA})
                match = re.search(r'"room_id":"(\d+)"', resp.text)
                if match: room_id = match.group(1)
            except Exception: pass
        print(f"[抖音DEBUG] raw keys: {list(raw.keys())}, avatar相关: { {k:v for k,v in raw.items() if 'avatar' in k.lower() or 'head' in k.lower() or 'img' in k.lower() or 'cover' in k.lower()} }")
        return {"streams": streams, "title": raw.get("anchor_name", "抖音主播"),
                "avatar": raw.get("avatar") or raw.get("avatar_thumb") or raw.get("head_img_url") or raw.get("cover") or "", "roomId": room_id, "isLive": True}
    except Exception as e:
        print(f"[抖音] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== Twitch ====================
async def parse_twitch(url, cookie: str = ""):
    try:
        match = re.search(r"twitch\.tv/([^/?]+)", url)
        if not match:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        channel = match.group(1)
        client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
        eff_cookie = cookie or TWITCH_COOKIE
        headers = {"Client-ID": client_id, "User-Agent": UA}
        if eff_cookie:
            headers["Cookie"] = eff_cookie

        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        # 获取头像和昵称
        nickname = channel
        avatar = ""
        try:
            user_url = f"https://api.twitch.tv/helix/users?login={channel}"
            user_resp = await request_with_proxy_group("GET", user_url, proxy_list=proxylist,
                                                       headers=headers, shuffle_proxy=False)
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                ud = user_data.get("data", [])
                if ud:
                    avatar = ud[0].get("profile_image_url", "")
                    nickname = ud[0].get("display_name", channel)
        except Exception:
            pass

        # 获取播放 token
        gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
        if eff_cookie:
            gql_headers["Cookie"] = eff_cookie
        gql_url = "https://gql.twitch.tv/gql"
        payload = [{"operationName": "PlaybackAccessToken", "variables": {"login": channel, "playerType": "site"},
                     "query": "query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: \"web\", playerType: $playerType, playerBackend: \"mediaplayer\" }) { value signature } }"}]
        resp = await request_with_proxy_group("POST", gql_url, proxy_list=proxylist, json=payload,
                                             headers=gql_headers, shuffle_proxy=False)
        if resp.status_code != 200:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        data = resp.json()
        token = sig = None
        if isinstance(data, list) and len(data) > 0:
            t = data[0].get("data", {}).get("streamPlaybackAccessToken")
            if t: token, sig = t.get("value"), t.get("signature")
        if not token or not sig:
            return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}&allow_source=true&allow_audio_only=true&allow_spectre=true&fast_bread=true"
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist,
                                                     headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"},
                                                     shuffle_proxy=False)
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
        print(f"[Twitch] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== SOOP ====================
async def parse_soop(url, cookie: str = ""):
    try:
        eff_cookie = cookie or SOOP_COOKIE
        cache_key = url + ("_auth" if eff_cookie else "")
        cached = M3U8_CACHE.get(cache_key)
        if cached and cached["expire"] > time.time():
            return cached["data"]

        parts = url.rstrip('/').split('/')
        bj_id = parts[3].split('?')[0] if len(parts) > 3 else parts[-1].split('?')[0]

        headers_pc = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://play.sooplive.com',
            'referer': 'https://play.sooplive.com',
        }
        if eff_cookie:
            headers_pc['cookie'] = eff_cookie

        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        # 1. 获取主播昵称+头像（get_station_info）
        nickname = f'BJ-{bj_id}'
        avatar = ''
        try:
            info_api = f'https://st.sooplive.com/api/get_station_info.php?szBjId={bj_id}'
            info_resp = await request_with_proxy_group("GET", info_api, proxy_list=proxylist,
                                                       headers=headers_pc, shuffle_proxy=False)
            if info_resp.status_code == 200:
                si = info_resp.json()
                station = si.get('station', {})
                nickname = station.get('user_nick', nickname)
                avatar = station.get('profile_image', '')
        except Exception:
            pass

        # 2. 直播状态
        live_api = f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}'
        live_data_form = {
            'bid': bj_id, 'bno': '', 'type': '', 'pwd': '',
            'player_type': 'html5', 'stream_type': 'common', 'quality': 'master',
            'mode': 'landing', 'from_api': '0', 'is_revive': 'false',
        }
        live_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                   headers=headers_pc, data=live_data_form, shuffle_proxy=False)
        if live_resp.status_code != 200:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result
        live_json = live_resp.json()
        channel = live_json.get('CHANNEL', {})
        result_code = channel.get('RESULT', -1)
        if result_code == -6:
            return {"streams": [], "isLive": False, "error": "19+成年直播间，请在设置中填入SOOP登录Cookie",
                    "title": nickname, "avatar": avatar}
        if result_code not in [0, 1]:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        broad_no = channel.get('BNO', '')
        if not broad_no:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        # 3. 获取 view_url 与 aid
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
            proxy_list=proxylist, headers=headers_pc, params=cdn_params, shuffle_proxy=False)
        if cdn_resp.status_code != 200:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result
        cdn_json = cdn_resp.json()
        view_url = cdn_json.get('view_url')
        if not view_url:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        aid_form = live_data_form.copy()
        aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", live_api, proxy_list=proxylist,
                                                  headers=headers_pc, data=aid_form, shuffle_proxy=False)
        if aid_resp.status_code != 200:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result
        aid_json = aid_resp.json()
        aid = aid_json.get('CHANNEL', {}).get('AID', '')
        if not aid:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        m3u8_url = f'{view_url}?aid={aid}'
        streams = []
        try:
            master_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist,
                                                         headers={"User-Agent": UA, "Referer": "https://play.sooplive.com"},
                                                         shuffle_proxy=False)
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
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
                                sub_url = urljoin(m3u8_url, sub_url)
                            streams.append({"cdn": f"SOOP-{name}", "url": sub_url, "type": "m3u8"})
                if streams:
                    streams.sort(key=lambda s: float(s['cdn'].replace('SOOP-','').replace('p','').replace('k','000')), reverse=True)
        except Exception:
            pass
        if not streams:
            streams = [{"cdn": "SOOP-Source", "url": m3u8_url, "type": "m3u8"}]

        result = {"streams": streams, "title": nickname, "avatar": avatar, "isLive": True}
        M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
        return result
    except Exception as e:
        print(f"[SOOP] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== PandaTV ====================
async def parse_panda(url):
    return await parse_panda_manual(url)

async def parse_panda_manual(url):
    try:
        cached = M3U8_CACHE.get(url)
        if cached and cached["expire"] > time.time():
            return cached["data"]

        user_id = url.split('?')[0].rstrip('/').split('/')[-1]
        headers = {'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/', 'user-agent': UA}
        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        # 1. 主播信息
        info_url = 'https://api.pandalive.co.kr/v1/member/bj'
        resp = await request_with_proxy_group("POST", info_url, proxy_list=proxylist,
                                              headers=headers,
                                              data={'userId': user_id, 'info': 'media fanGrade'},
                                              shuffle_proxy=False)
        if resp.status_code != 200:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        info_json = resp.json()
        if 'bjInfo' not in info_json:
            return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        bj_info = info_json['bjInfo']
        anchor_name = bj_info.get('nick', user_id)
        avatar = bj_info.get('profileImg', '')  # 根据实际字段调整，可能是 profileImg

        if 'media' not in info_json:
            result = {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
            M3U8_CACHE[url] = {"data": result, "expire": time.time() + 30}
            return result

        # 2. 播放地址
        play_url = 'https://api.pandalive.co.kr/v1/live/play'
        resp2 = await request_with_proxy_group("POST", play_url, proxy_list=proxylist,
                                               headers=headers,
                                               data={'action': 'watch', 'userId': user_id, 'password': '', 'shareLinkType': ''},
                                               shuffle_proxy=False)
        if resp2.status_code != 200:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        play_json = resp2.json()
        if 'errorData' in play_json:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        if 'PlayList' not in play_json or 'hls' not in play_json['PlayList']:
            return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        real_m3u8 = play_json['PlayList']['hls'][0]['url']

        # 3. 多画质
        streams = []
        try:
            cf_worker = CF_WORKER.rstrip("/") if CF_WORKER else ""
            if cf_worker:
                fetch_url = f"{cf_worker}?url={quote(real_m3u8, safe='')}&referer={quote('https://www.pandalive.co.kr/', safe='')}"
                master_resp = await request_with_proxy_group("GET", fetch_url, proxy_list=[None],
                                                             headers={"User-Agent": UA}, shuffle_proxy=False)
            else:
                master_resp = await request_with_proxy_group("GET", real_m3u8, proxy_list=proxylist,
                                                             headers={"User-Agent": UA, "Referer": "https://www.pandalive.co.kr/",
                                                                      "Origin": "https://www.pandalive.co.kr"},
                                                             shuffle_proxy=False)
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
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
                                sub_url = urljoin(real_m3u8, sub_url)
                            streams.append({"cdn": f"PandaTV-{name}", "url": sub_url, "type": "m3u8"})
                if streams:
                    streams.sort(key=lambda s: float(s['cdn'].replace('PandaTV-','').replace('p','').replace('k','000')), reverse=True)
        except Exception:
            pass
        if not streams:
            streams = [{"cdn": "PandaTV-Source", "url": real_m3u8, "type": "m3u8"}]

        result = {"streams": streams, "title": anchor_name, "avatar": avatar, "isLive": True}
        M3U8_CACHE[url] = {"data": result, "expire": time.time() + 30}
        return result
    except Exception as e:
        print(f"[PandaTV] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

@app.get("/api/parse")
async def api_parse(url: str = Query(...), cookie: str = Query("")):
    try:
        if "huya.com" in url: return await parse_huya(url)
        if "douyu.com" in url: return await parse_douyu(url)
        if "bilibili.com" in url: return await parse_bilibili(url)
        if "douyin.com" in url: return await parse_douyin(url)
        if "twitch.tv" in url: return await parse_twitch(url, cookie=cookie)
        if "sooplive.com" in url: return await parse_soop(url, cookie=cookie)
        if "pandalive.co.kr" in url: return await parse_panda(url)
        raise HTTPException(400, "不支持的平台")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ==================== 弹幕代理（不变） ====================
@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    await websocket.accept()
    go_ws_url = f"ws://localhost:1088/ws/{room_id}"
    try:
        async with websockets.connect(go_ws_url, ping_interval=None) as go_ws:
            async def forward_to_go():
                while True:
                    data = await websocket.receive_text()
                    if data == 'ping':
                        try: await go_ws.ping()
                        except: pass
                        await websocket.send_text('pong')
                    elif go_ws.open:
                        await go_ws.send(data)
            async def forward_to_frontend():
                while True:
                    data = await go_ws.recv()
                    if isinstance(data, bytes): data = data.decode("utf-8")
                    await websocket.send_text(data)
            await asyncio.gather(forward_to_go(), forward_to_frontend(), return_exceptions=True)
    except Exception as e:
        print(f"[WS] 抖音代理异常: {e}")
        try: await websocket.close()
        except: pass

@app.websocket("/ws/twitch/{channel_name}")
async def websocket_twitch_danmaku(ws_conn: WebSocket, channel_name: str):
    await ws_conn.accept()
    if not WEBSOCKET_CLIENT_AVAILABLE:
        await ws_conn.close()
        return
    stop_event = threading.Event()
    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    def on_msg(ws, msg):
        if msg.startswith("PING"): ws.send("PONG :tmi.twitch.tv"); return
        if msg.startswith("PONG"): return
        m = re.match(r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)", msg)
        if m:
            asyncio.run_coroutine_threadsafe(queue.put({"type":"chat","nick":m.group(1),"content":m.group(2)}), loop)
    def run():
        ws = websocket_client.WebSocketApp("wss://irc-ws.chat.twitch.tv:443",
                                     on_message=on_msg,
                                     on_error=lambda w,e: print(f"Twitch IRC err: {e}"),
                                     on_close=lambda w,c,m: print("Twitch IRC closed"))
        ws.on_open = lambda w: (w.send("CAP REQ :twitch.tv/tags twitch.tv/commands"),
                                w.send("PASS SCHMOOPIIE"), w.send("NICK justinfan12345"),
                                w.send(f"JOIN #{channel_name.lower()}"))
        ws.run_forever()
    task = loop.run_in_executor(None, run)
    async def sender():
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(queue.get(), 1)
                await ws_conn.send_json(msg)
            except: pass
    send_task = asyncio.create_task(sender())
    try:
        while True:
            data = await ws_conn.receive_text()
            if data == "ping": await ws_conn.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        stop_event.set()
        send_task.cancel()
        try: await send_task
        except: pass
        task.cancel()

@app.get("/")
def root(): return {"status":"ok"}

@app.api_route("/health", methods=["GET","HEAD"])
async def health(): return {"status":"alive"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)