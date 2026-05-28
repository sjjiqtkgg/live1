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

# 提取公共 MD5 方法以优化冗余逻辑
def md5_hex(data: str) -> str:
    return hashlib.md5(data.encode('utf-8')).hexdigest()

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
    import websocket as websocket_client
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

# ==================== 代理与全局配置 ====================
PROXY_LIST_STR = os.getenv("PROXY_LIST", "")
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()] if PROXY_LIST_STR else [None]
EXTERNAL_PROXY_LIST_STR = os.getenv("EXTERNAL_PROXY_LIST", "")
EXTERNAL_PROXY_URLS = [p.strip() for p in EXTERNAL_PROXY_LIST_STR.split(",") if p.strip()] if EXTERNAL_PROXY_LIST_STR else [None]
CF_WORKER = os.getenv("CF_WORKER_URL", "")
SOOP_COOKIE = os.getenv("SOOP_COOKIE", "")
TWITCH_COOKIE = os.getenv("TWITCH_COOKIE", "")

CLIENT_POOL: dict = {}
CLIENT_LOCK = asyncio.Lock()
DEFAULT_TIMEOUT = 15
STREAM_PROXY_MAP: dict = {}
M3U8_CACHE: dict = {}

def get_fixed_proxy_list(proxy_pool):
    if not proxy_pool or proxy_pool[0] is None: return [None]
    primary = random.choice(proxy_pool)
    return [primary] + [p for p in proxy_pool if p != primary]

async def get_client(proxy=None, timeout=None):
    if timeout is None: timeout = DEFAULT_TIMEOUT
    key = f"{proxy or 'direct'}_t{timeout}"
    async with CLIENT_LOCK:
        if key not in CLIENT_POOL:
            CLIENT_POOL[key] = httpx.AsyncClient(timeout=timeout, proxy=proxy, http2=True, verify=False, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        return CLIENT_POOL[key]

async def request_with_retry(method, url, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for proxy in PROXY_URLS:
        try:
            client = await get_client(proxy, timeout)
            return await client.request(method, url, **kwargs)
        except Exception as e:
            last_error = e
    raise last_error or Exception("所有代理均失败")

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    shuffle_proxy = kwargs.pop("shuffle_proxy", False)
    targets = proxy_list[:]
    if shuffle_proxy and targets: random.shuffle(targets)
    for proxy in targets:
        try:
            client = await get_client(proxy, timeout)
            return await client.request(method, url, **kwargs)
        except Exception as e:
            last_error = e
    raise last_error or Exception("所有代理均失败")

@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = [
        ".douyu.com", ".huya.com", ".bilibili.com", ".bilivideo.com", ".douyucdn.cn",
        ".douyin.com", ".live.bilibili.com", ".twitch.tv", ".ttvnw.net",
        ".sooplive.com", ".sooplive.net", ".sooplivecdn.com",
        ".pandalive.co.kr", ".live-video.net", ".pandalivecdn.com",
    ]
    if not any(urlparse(url).hostname.endswith(domain) for domain in ALLOWED):
        raise HTTPException(403, "domain not allowed")

    body = await request.body() if request.method == "POST" else None
    headers = {"User-Agent": ua or UA, "Referer": referer or ""}
    if cookie: headers["Cookie"] = cookie
    if request.method == "POST": headers["Content-Type"] = "application/x-www-form-urlencoded"

    EXTERNAL_DOMAINS = ["twitch.tv", "ttvnw.net", "twitchsvc.net", "sooplive.com", "livestream-manager.sooplive.com", "pandalive.co.kr", "live-video.net", "pandalivecdn.com"]
    EXTERNAL_REFERERS = ["twitch.tv", "player.twitch.tv", "sooplive.com", "pandalive.co.kr"]
    use_external = any(d in url for d in EXTERNAL_DOMAINS) or any(d in (referer or "") for d in EXTERNAL_REFERERS)
    proxy_list = EXTERNAL_PROXY_URLS if use_external else PROXY_URLS
    is_ts = url.lower().endswith(".ts")

    if is_ts:
        stream_key = referer or url
        if stream_key not in STREAM_PROXY_MAP: STREAM_PROXY_MAP[stream_key] = random.choice(proxy_list) if (proxy_list and proxy_list[0]) else None
        proxies_to_use = [STREAM_PROXY_MAP[stream_key]] if STREAM_PROXY_MAP[stream_key] else proxy_list
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
        
        eff_referer = referer if referer else ("https://www.pandalive.co.kr/" if "pandalive" in url or "live-video.net" in url else "https://player.twitch.tv" if "twitch" in url or "ttvnw" in url else "https://play.sooplive.com")
        rewritten = []
        for line in resp.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                abs_url = stripped if stripped.startswith("http") else (cdn_origin + stripped if stripped.startswith("/") else base_url + stripped)
                rewritten.append(f"{proxy_base}?url={quote(abs_url, safe='')}&referer={quote(eff_referer, safe='')}")
            else:
                rewritten.append(line)
        return StreamingResponse(iter(["\n".join(rewritten).encode("utf-8")]), status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/vnd.apple.mpegurl"})

    if is_ts:
        return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "video/mp2t"})
    return StreamingResponse(iter([resp.content]), status_code=resp.status_code, headers={"Access-Control-Allow-Origin": "*", "Content-Type": content_type or "application/json"})

# ==================== 虎牙 ====================
async def fetch_huya_danmaku_params(room_id):
    try:
        resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}", headers={"User-Agent": MOBILE_UA, "Referer": "https://www.huya.com/"})
        html = resp.text
        ayyuid = int((re.search(r'"lYyid":(\d+)', html) or re.search(r'ayyuid:\s*["\']?(\d+)', html) or [None, 0])[1])
        top_sid = int((re.search(r'"lChannelId":(\d+)', html) or [None, 0])[1])
        sub_sid = int((re.search(r'"lSubChannelId":(\d+)', html) or [None, 0])[1])
        return {"platform": "huya", "ayyuid": ayyuid, "topSid": top_sid, "subSid": sub_sid}
    except Exception: return {}

def huya_build_anticode(raw_anti, stream_name):
    anti = raw_anti.replace("&amp;", "&")
    params = dict(p.split("=", 1) for p in anti.split("&") if "=" in p)
    fm, ws_time = params.get("fm", ""), params.get("wsTime", "")
    if not fm or not ws_time: return anti
    try: fm_dec = base64.b64decode(fm.replace("%2B", "+").replace("%2F", "/").replace("%3D", "=") + "==").decode()
    except Exception:
        try: fm_dec = base64.b64decode(unquote(fm) + "==").decode()
        except Exception: return anti
    p = fm_dec.split("_")[0]
    seqid = str(int(time.time() * 10000 + random.random() * 10000))
    ws_secret = md5_hex(f"{p}_0_{stream_name}_{seqid}_{ws_time}")
    params["wsSecret"] = ws_secret
    params["seqid"] = seqid
    params["u"] = "0"
    return "&".join(f"{k}={v}" for k, v in params.items())

async def parse_huya(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        CDN_NAMES = {"AL": "阿里", "TX": "腾讯", "HW": "华为", "WS": "网宿", "BD": "百度"}
        CDN_ORDER = {"TX": 0, "AL": 1, "HW": 2, "WS": 3, "BD": 4}
        resp = await request_with_retry("GET", f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}", headers={"User-Agent": UA, "Referer": "https://www.huya.com/"})
        data = resp.json()
        if data.get("status") != 200: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        live = data["data"]

        profile, room_info, live_data, anchor = live.get("profileRoom", {}), live.get("roomInfo", {}), live.get("liveData", {}), live.get("anchor", {})
        anchor_name = profile.get("nick") or room_info.get("nick") or live_data.get("nick") or anchor.get("nick") or profile.get("sNick") or room_info.get("sNick") or ""
        if not anchor_name:
            try:
                mob_html = (await request_with_retry("GET", f"https://m.huya.com/{room_id}", headers={"User-Agent": MOBILE_UA, "Referer": "https://www.huya.com/"})).text
                m = re.search(r'"nick":"([^"]+)"', mob_html) or re.search(r'<title>([^_<]+)', mob_html)
                if m: anchor_name = m.group(1).strip()
            except Exception: pass
        anchor_name = anchor_name or "虎牙主播"
        avatar = profile.get("avatar180") or profile.get("sAvatar180") or anchor.get("avatar180") or room_info.get("avatar180") or ""

        if live.get("realLiveStatus") != "ON": return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}

        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        if not cdn_list: return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        cdn_list.sort(key=lambda s: CDN_ORDER.get(s.get("sCdnType", "ZZ"), 9))
        
        streams, seen_urls = [], set()
        # 双重循环：遍历最优的 2 个 CDN 节点，并分别计算 多画质 的签名
        for s in cdn_list[:2]:
            cdn_type = s.get("sCdnType", "")
            flv_url = s.get("sFlvUrl", "")
            stream_name = s.get("sStreamName", "")
            anti_code = s.get("sFlvAntiCode", "")
            suffix = s.get("sFlvUrlSuffix", "flv")
            if not (flv_url and stream_name and anti_code): continue
            
            # 画质码率组合 (权重大排前)
            for suffix_str, q_name, weight in [("", "原画", 3), ("_2000", "超清", 2), ("_1200", "高清", 1)]:
                target_stream = f"{stream_name}{suffix_str}"
                built = huya_build_anticode(anti_code, target_stream)
                full_url = f"{flv_url}/{target_stream}.{suffix}?{built}".replace("http://", "https://")
                
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    label = f"{CDN_NAMES.get(cdn_type, cdn_type)}-{q_name}"
                    streams.append({"cdn": label, "url": full_url, "type": "flv", "weight": weight})

        if not streams: return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        # 统一按画质权重降序，确保最前面是原画
        streams.sort(key=lambda x: x.get('weight', 0), reverse=True)
        for s in streams: s.pop('weight', None)

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
        room = (await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)).json().get("room")
        if not room: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        name, avatar = room.get("nickname") or "斗鱼主播", room.get("room_icon", "")
        if isinstance(avatar, dict): avatar = avatar.get("big") or avatar.get("middle") or ""
        if room.get("show_status") != 1 or room.get("videoLoop") == 1: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        real_id, did = str(room["room_id"]), "10000000000000000000000000001501"
        enc_data = (await request_with_retry("GET", f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}", headers={"User-Agent": UA})).json()
        if enc_data.get("error") != 0: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        
        white = enc_data["data"]
        ts = int(time.time())
        secret = white['rand_str']
        for _ in range(white['enc_time']): secret = md5_hex(secret + white['key'])
        suffix = f"{real_id}{ts}" if not white.get('is_special', False) else ""
        auth = md5_hex(secret + white['key'] + suffix)
        
        base_params = {'ver': '219032101', 'iar': '0', 'ive': '0', 'rid': real_id, 'hevc': '0', 'fa': '0', 'sov': '0', 'enc_data': white['enc_data'], 'tt': str(ts), 'did': did, 'auth': auth}
        
        streams, seen_urls = [], set()
        # 遍历请求多个画质：0=原画, 2=超清, 4=标清
        for rate, q_name, weight in [("0", "原画", 3), ("2", "超清", 2), ("4", "标清", 1)]:
            params = base_params.copy()
            params['rate'] = rate
            stream_data = (await request_with_retry("POST", f"https://playweb.douyucdn.cn/lapi/live/getH5PlayV1/{real_id}", headers=hdrs, data=params)).json()
            if stream_data.get("error") != 0: continue
            
            info_stream = stream_data["data"]
            flv_url = f"{info_stream['rtmp_url']}/{info_stream['rtmp_live']}" if info_stream.get('rtmp_url') and info_stream.get('rtmp_live') else None
            hls_url = info_stream.get('hls_url')
            
            if flv_url and flv_url not in seen_urls:
                seen_urls.add(flv_url)
                streams.append({"cdn": q_name, "url": flv_url, "type": "flv", "weight": weight})
            if hls_url and hls_url.startswith("http") and hls_url not in seen_urls:
                seen_urls.add(hls_url)
                streams.append({"cdn": f"{q_name}(HLS)", "url": hls_url, "type": "m3u8", "weight": weight - 0.1})

        if not streams: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        
        streams.sort(key=lambda x: x.get('weight', 0), reverse=True)
        for s in streams: s.pop('weight', None)
        return {"streams": streams, "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[斗鱼] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== B站 ====================
async def parse_bilibili(url):
    try:
        rid = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
        room_data = (await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers=hdrs)).json()
        if room_data.get("code") != 0: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        real_rid = room_data["data"]["room_id"]

        name, avatar = "B站主播", ""
        try:
            ir = (await request_with_retry("GET", f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}", headers=hdrs)).json()
            ri = ir.get("data", {}).get("room_info", {})
            name, avatar = ri.get("uname") or name, ri.get("face") or ""
        except Exception: pass

        if room_data["data"].get("live_status") != 1: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}

        play = (await request_with_retry("GET", f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8", headers=hdrs)).json()
        if play.get("code") != 0: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
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
                            streams.append({"cdn": f"{fmt['format_name'].upper()}-{m.group(1) if m else 'cdn'}", "url": u, "type": "flv" if fmt["format_name"] == "flv" else "m3u8"})
        streams.sort(key=lambda x: 0 if x["type"] == "flv" else 1)
        if not streams: return {"streams": [], "isLive": False, "title": name, "avatar": avatar}
        return {"streams": streams[:4], "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[B站] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== 抖音 ====================
async def parse_douyin(url):
    try:
        from streamget import DouyinLiveStream
        live = DouyinLiveStream()
        # 复用原始数据对象，防止发起过多请求被风控
        data = await live.fetch_web_stream_data(url, process_data=True)
        
        streams, seen_urls = [], set()
        raw_info = {}
        
        # 遍历所有画质，通过 seen_urls 去除抖音重复的 FVL 冗余链接
        for q_code, q_name, weight in [("OD", "原画", 4), ("HD", "超清", 3), ("SD", "高清", 2), ("LD", "标清", 1)]:
            try:
                stream_obj = await live.fetch_stream_url(data, q_code)
                if not stream_obj: continue
                raw = json.loads(stream_obj.to_json())
                raw_info = raw if not raw_info else raw_info
                
                flv_url, m3u8_url = raw.get("flv_url", ""), raw.get("m3u8_url", "")
                
                if flv_url and flv_url not in seen_urls:
                    seen_urls.add(flv_url)
                    streams.append({"cdn": f"{q_name}(FLV)", "url": flv_url, "type": "flv", "weight": weight})
                if m3u8_url and m3u8_url not in seen_urls:
                    seen_urls.add(m3u8_url)
                    streams.append({"cdn": f"{q_name}(HLS)", "url": m3u8_url, "type": "m3u8", "weight": weight - 0.1})
            except Exception: continue

        if not streams: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        
        # 降序排列，保证前端下标 0 是原画
        streams.sort(key=lambda x: x.get('weight', 0), reverse=True)
        for s in streams: s.pop('weight', None)

        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit():
            try: room_id = re.search(r'"room_id":"(\d+)"', (await request_with_retry("GET", url, headers={"User-Agent": UA})).text).group(1)
            except Exception: pass
            
        avatar = ""
        try:
            wc_data = (await request_with_retry("GET", f"https://webcast.amemv.com/douyin/webcast/reflow/{room_id}", headers={"User-Agent": UA, "Referer": "https://live.douyin.com/"})).json()
            url_list = wc_data.get("data", {}).get("room", {}).get("owner", {}).get("avatarThumb", {}).get("urlList", [])
            if url_list: avatar = url_list[0]
        except Exception: pass

        return {"streams": streams, "title": raw_info.get("anchor_name", "抖音主播"), "avatar": avatar, "roomId": room_id, "isLive": True}
    except Exception as e:
        print(f"[抖音] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== Twitch ====================
async def parse_twitch(url, cookie: str = ""):
    try:
        match = re.search(r"twitch\.tv/([^/?]+)", url)
        if not match: return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        channel = match.group(1)
        client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
        eff_cookie = cookie or TWITCH_COOKIE
        headers = {"Client-ID": client_id, "User-Agent": UA}
        if eff_cookie: headers["Cookie"] = eff_cookie

        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        nickname, avatar = channel, ""
        try:
            user_resp = await request_with_proxy_group("GET", f"https://api.twitch.tv/helix/users?login={channel}", proxy_list=proxylist, headers=headers, shuffle_proxy=False)
            if user_resp.status_code == 200:
                ud = user_resp.json().get("data", [])
                if ud: avatar, nickname = ud[0].get("profile_image_url", ""), ud[0].get("display_name", channel)
        except Exception: pass

        gql_headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
        if eff_cookie: gql_headers["Cookie"] = eff_cookie
        payload = [{"operationName": "PlaybackAccessToken", "variables": {"login": channel, "playerType": "site"}, "query": "query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: \"web\", playerType: $playerType, playerBackend: \"mediaplayer\" }) { value signature } }"}]
        resp = await request_with_proxy_group("POST", "https://gql.twitch.tv/gql", proxy_list=proxylist, json=payload, headers=gql_headers, shuffle_proxy=False)
        if resp.status_code != 200: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
        data = resp.json()
        token = sig = None
        if isinstance(data, list) and len(data) > 0:
            t = data[0].get("data", {}).get("streamPlaybackAccessToken")
            if t: token, sig = t.get("value"), t.get("signature")
        if not token or not sig: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}&allow_source=true&allow_audio_only=true&allow_spectre=true&fast_bread=true"
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist, headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"}, shuffle_proxy=False)
        if usher_resp.status_code != 200 or "#EXT-X-STREAM-INF" not in usher_resp.text: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}

        streams, lines = [], usher_resp.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                name = line.split("RESOLUTION=")[1].split(",")[0].replace("x", "p") if "RESOLUTION=" in line else "source"
                if i+1 < len(lines):
                    sub_url = lines[i+1].strip()
                    streams.append({"cdn": f"Twitch-{name}", "url": sub_url if sub_url.startswith("http") else urljoin(m3u8_url, sub_url), "type": "m3u8"})
        streams.sort(key=lambda s: (0 if "source" in s["cdn"] else 1, s["cdn"]))
        if not streams: return {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
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
        if cached and cached["expire"] > time.time(): return cached["data"]

        parts = url.rstrip('/').split('/')
        bj_id = parts[3].split('?')[0] if len(parts) > 3 else parts[-1].split('?')[0]
        headers_pc = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0', 'content-type': 'application/x-www-form-urlencoded; charset=UTF-8', 'origin': 'https://play.sooplive.com', 'referer': 'https://play.sooplive.com'}
        if eff_cookie: headers_pc['cookie'] = eff_cookie

        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)
        nickname, avatar = f'BJ-{bj_id}', ''
        try:
            info_resp = await request_with_proxy_group("GET", f'https://st.sooplive.com/api/get_station_info.php?szBjId={bj_id}', proxy_list=proxylist, headers=headers_pc, shuffle_proxy=False)
            if info_resp.status_code == 200:
                station = info_resp.json().get('station', {})
                nickname, avatar = station.get('user_nick', nickname), station.get('profile_image', '')
        except Exception: pass

        live_data_form = {'bid': bj_id, 'bno': '', 'type': '', 'pwd': '', 'player_type': 'html5', 'stream_type': 'common', 'quality': 'master', 'mode': 'landing', 'from_api': '0', 'is_revive': 'false'}
        live_resp = await request_with_proxy_group("POST", f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}', proxy_list=proxylist, headers=headers_pc, data=live_data_form, shuffle_proxy=False)
        if live_resp.status_code != 200:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result
        
        channel = live_resp.json().get('CHANNEL', {})
        result_code = channel.get('RESULT', -1)
        if result_code == -6: return {"streams": [], "isLive": False, "error": "19+成年直播间，请在设置中填入SOOP登录Cookie", "title": nickname, "avatar": avatar}
        
        broad_no = channel.get('BNO', '')
        if result_code not in [0, 1] or not broad_no:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        cdn_params = {'return_type': 'gcp_cdn', 'use_cors': 'false', 'cors_origin_url': 'play.sooplive.com', 'broad_key': f'{broad_no}-common-master-hls', 'time': str(time.time())}
        cdn_resp = await request_with_proxy_group("GET", 'http://livestream-manager.sooplive.com/broad_stream_assign.html', proxy_list=proxylist, headers=headers_pc, params=cdn_params, shuffle_proxy=False)
        if cdn_resp.status_code != 200 or not cdn_resp.json().get('view_url'):
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        aid_form = live_data_form.copy()
        aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}', proxy_list=proxylist, headers=headers_pc, data=aid_form, shuffle_proxy=False)
        aid = aid_resp.json().get('CHANNEL', {}).get('AID', '') if aid_resp.status_code == 200 else ''
        if not aid:
            result = {"streams": [], "isLive": False, "title": nickname, "avatar": avatar}
            M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
            return result

        m3u8_url = f'{cdn_resp.json().get("view_url")}?aid={aid}'
        streams = []
        try:
            master_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=proxylist, headers={"User-Agent": UA, "Referer": "https://play.sooplive.com"}, shuffle_proxy=False)
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        name = re.search(r'RESOLUTION=(\d+x\d+)', line).group(1).split('x')[1] + 'p' if re.search(r'RESOLUTION=(\d+x\d+)', line) else (f"{int(int(re.search(r'BANDWIDTH=(\d+)', line).group(1))/1000)}k" if re.search(r'BANDWIDTH=(\d+)', line) else "Source")
                        if i + 1 < len(lines) and lines[i+1].strip():
                            streams.append({"cdn": f"SOOP-{name}", "url": lines[i+1].strip() if lines[i+1].strip().startswith("http") else urljoin(m3u8_url, lines[i+1].strip()), "type": "m3u8"})
                if streams: streams.sort(key=lambda s: 999999 if 'Source' in s['cdn'] else float(re.search(r'\d+', s['cdn']).group() if re.search(r'\d+', s['cdn']) else 0), reverse=True)
        except Exception: pass
        if not streams: streams = [{"cdn": "SOOP-Source", "url": m3u8_url, "type": "m3u8"}]

        result = {"streams": streams, "title": nickname, "avatar": avatar, "isLive": True}
        M3U8_CACHE[cache_key] = {"data": result, "expire": time.time() + 30}
        return result
    except Exception as e:
        print(f"[SOOP] 解析异常: {e}")
        return {"streams": [], "isLive": False, "title": "", "avatar": ""}

# ==================== PandaTV ====================
async def parse_panda(url): return await parse_panda_manual(url)

async def parse_panda_manual(url):
    try:
        cached = M3U8_CACHE.get(url)
        if cached and cached["expire"] > time.time(): return cached["data"]

        user_id = url.split('?')[0].rstrip('/').split('/')[-1]
        headers = {'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/', 'user-agent': UA}
        proxylist = get_fixed_proxy_list(EXTERNAL_PROXY_URLS)

        resp = await request_with_proxy_group("POST", 'https://api.pandalive.co.kr/v1/member/bj', proxy_list=proxylist, headers=headers, data={'userId': user_id, 'info': 'media fanGrade'}, shuffle_proxy=False)
        if resp.status_code != 200 or 'bjInfo' not in resp.json(): return {"streams": [], "isLive": False, "title": "", "avatar": ""}
        bj_info = resp.json()['bjInfo']
        anchor_name, avatar = bj_info.get('nick', user_id), bj_info.get('profileImg', '')

        if 'media' not in resp.json():
            result = {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
            M3U8_CACHE[url] = {"data": result, "expire": time.time() + 30}
            return result

        play_json = (await request_with_proxy_group("POST", 'https://api.pandalive.co.kr/v1/live/play', proxy_list=proxylist, headers=headers, data={'action': 'watch', 'userId': user_id, 'password': '', 'shareLinkType': ''}, shuffle_proxy=False)).json()
        if 'errorData' in play_json or 'PlayList' not in play_json or 'hls' not in play_json['PlayList']: return {"streams": [], "isLive": False, "title": anchor_name, "avatar": avatar}
        real_m3u8 = play_json['PlayList']['hls'][0]['url']

        streams = []
        try:
            cf_worker = CF_WORKER.rstrip("/") if CF_WORKER else ""
            if cf_worker:
                master_resp = await request_with_proxy_group("GET", f"{cf_worker}?url={quote(real_m3u8, safe='')}&referer={quote('https://www.pandalive.co.kr/', safe='')}", proxy_list=[None], headers={"User-Agent": UA}, shuffle_proxy=False)
            else:
                master_resp = await request_with_proxy_group("GET", real_m3u8, proxy_list=proxylist, headers={"User-Agent": UA, "Referer": "https://www.pandalive.co.kr/", "Origin": "https://www.pandalive.co.kr"}, shuffle_proxy=False)
            
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        name = re.search(r'RESOLUTION=(\d+x\d+)', line).group(1).split('x')[1] + 'p' if re.search(r'RESOLUTION=(\d+x\d+)', line) else (f"{int(int(re.search(r'BANDWIDTH=(\d+)', line).group(1))/1000)}k" if re.search(r'BANDWIDTH=(\d+)', line) else "Source")
                        if i + 1 < len(lines) and lines[i+1].strip():
                            streams.append({"cdn": f"PandaTV-{name}", "url": lines[i+1].strip() if lines[i+1].strip().startswith("http") else urljoin(real_m3u8, lines[i+1].strip()), "type": "m3u8"})
                if streams: streams.sort(key=lambda s: 999999 if 'Source' in s['cdn'] else float(re.search(r'\d+', s['cdn']).group() if re.search(r'\d+', s['cdn']) else 0), reverse=True)
        except Exception: pass
        if not streams: streams = [{"cdn": "PandaTV-Source", "url": real_m3u8, "type": "m3u8"}]

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
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

# ==================== 弹幕代理 ====================
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
                    elif go_ws.open: await go_ws.send(data)
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
        if m: asyncio.run_coroutine_threadsafe(queue.put({"type":"chat","nick":m.group(1),"content":m.group(2)}), loop)
    def run():
        ws = websocket_client.WebSocketApp("wss://irc-ws.chat.twitch.tv:443", on_message=on_msg, on_error=lambda w,e: print(f"Twitch IRC err: {e}"), on_close=lambda w,c,m: print("Twitch IRC closed"))
        ws.on_open = lambda w: (w.send("CAP REQ :twitch.tv/tags twitch.tv/commands"), w.send("PASS SCHMOOPIIE"), w.send("NICK justinfan12345"), w.send(f"JOIN #{channel_name.lower()}"))
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
    except WebSocketDisconnect: pass
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
