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
from fastapi.responses import StreamingResponse
from urllib.parse import unquote, urlparse, parse_qs, quote

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

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36"

# ==================== 代理配置 ====================
PROXY_LIST_STR = os.getenv("PROXY_LIST", "")
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()] if PROXY_LIST_STR else [None]
print(f"[代理] 国内代理 {len(PROXY_URLS)} 个: {PROXY_URLS}")

EXTERNAL_PROXY_LIST_STR = os.getenv("EXTERNAL_PROXY_LIST", "")
EXTERNAL_PROXY_URLS = [p.strip() for p in EXTERNAL_PROXY_LIST_STR.split(",") if p.strip()] if EXTERNAL_PROXY_LIST_STR else [None]
print(f"[代理] 外网代理 {len(EXTERNAL_PROXY_URLS)} 个: {EXTERNAL_PROXY_URLS}")

async def request_with_retry(method, url, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for idx, proxy in enumerate(PROXY_URLS):
        try:
            print(f"[请求重试] 尝试代理 [{idx+1}/{len(PROXY_URLS)}]: {proxy or '直连'}")
            async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
                resp = await client.request(method, url, **kwargs)
                return resp
        except Exception as e:
            last_error = e
            print(f"[请求重试] 失败: {e}")
    raise last_error or Exception("所有代理均失败")

async def request_with_proxy_group(method, url, proxy_list, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for idx, proxy in enumerate(proxy_list):
        try:
            print(f"[分组请求] 尝试代理 [{idx+1}/{len(proxy_list)}]: {proxy or '直连'}")
            async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
                resp = await client.request(method, url, **kwargs)
                return resp
        except Exception as e:
            last_error = e
            print(f"[分组请求] 失败: {e}")
    raise last_error or Exception("所有代理均失败")

@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = [
        ".douyu.com", ".huya.com", ".bilibili.com", ".bilivideo.com", ".douyucdn.cn",
        ".douyin.com", ".live.bilibili.com", ".twitch.tv", ".ttvnw.net",
        ".sooplive.com", ".sooplive.net", ".sooplivecdn.com",
        ".pandalive.co.kr",
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
        "pandalive.co.kr",
    ]
    EXTERNAL_REFERERS = ["twitch.tv", "player.twitch.tv", "sooplive.com", "pandalive.co.kr"]
    use_external = (
        any(d in url for d in EXTERNAL_DOMAINS) or
        any(d in (referer or "") for d in EXTERNAL_REFERERS)
    )
    proxy_list = EXTERNAL_PROXY_URLS if use_external else PROXY_URLS

    resp = await request_with_proxy_group(request.method, url, proxy_list=proxy_list, headers=headers, content=body)
    content_type = resp.headers.get("content-type", "")
    is_m3u8 = (
        "mpegurl" in content_type.lower()
        or url.split("?")[0].endswith(".m3u8")
    )
    if is_m3u8:
        # 重写 m3u8 中的相对/绝对 URL，使子播放列表和分片也走代理
        base_url = url.rsplit("/", 1)[0] + "/"
        parsed_cdn = urlparse(url)
        cdn_origin = f"{parsed_cdn.scheme}://{parsed_cdn.netloc}"
        # 用完整绝对 URL 避免 hls.js 相对路径解析错误
        proxy_base = str(request.base_url).rstrip("/") + "/api/proxy"
        eff_referer = referer or "https://play.sooplive.com"
        lines = resp.text.splitlines()
        rewritten = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("http://") or stripped.startswith("https://"):
                    abs_url = stripped
                elif stripped.startswith("/"):
                    # 以 / 开头的绝对路径，补全 CDN host
                    abs_url = cdn_origin + stripped
                else:
                    abs_url = base_url + stripped
                # safe='' 保证 URL 中的 : / ? = & 全部被编码
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
            return {"streams": [], "isLive": False}
        live = data["data"]
        if live.get("realLiveStatus") != "ON":
            return {"streams": [], "isLive": False}
        cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
        if not cdn_list:
            return {"streams": [], "isLive": False}
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
            return {"streams": [], "isLive": False}

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
        avatar = profile.get("avatar", "") or anchor.get("avatar", "")
        danmaku = await fetch_huya_danmaku_params(room_id)
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku, "isLive": True}
    except Exception as e:
        print(f"[虎牙] 解析异常: {e}")
        return {"streams": [], "isLive": False}

# ==================== 斗鱼（新接口） ====================
async def parse_douyu(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": f"https://www.douyu.com/{room_id}"}
        info_resp = await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)
        info = info_resp.json()
        room = info.get("room")
        if not room or room.get("show_status") != 1 or room.get("videoLoop") == 1:
            return {"streams": [], "isLive": False}
        real_id = str(room["room_id"])
        did = "10000000000000000000000000001501"
        enc_resp = await request_with_retry("GET", f"https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={did}",
                                            headers={"User-Agent": UA})
        enc_data = enc_resp.json()
        if enc_data.get("error") != 0:
            return {"streams": [], "isLive": False}
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
            return {"streams": [], "isLive": False}
        info_stream = stream_data["data"]
        flv_url = f"{info_stream['rtmp_url']}/{info_stream['rtmp_live']}" if info_stream.get('rtmp_url') and info_stream.get('rtmp_live') else None
        hls_url = info_stream.get('hls_url')
        streams = []
        if flv_url:
            streams.append({"cdn": "FLV", "url": flv_url, "type": "flv"})
        if hls_url and hls_url.startswith("http"):
            streams.append({"cdn": "HLS", "url": hls_url, "type": "m3u8"})
        if not streams:
            return {"streams": [], "isLive": False}
        name = room.get("nickname") or "斗鱼主播"
        avatar = room.get("room_icon", "")
        if isinstance(avatar, dict):
            avatar = avatar.get("big") or avatar.get("middle") or ""
        return {"streams": streams, "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[斗鱼] 解析异常: {e}")
        return {"streams": [], "isLive": False}

# ==================== B站 ====================
async def parse_bilibili(url):
    try:
        rid = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
        room_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers=hdrs)
        room_data = room_resp.json()
        if room_data.get("code") != 0: return {"streams": [], "isLive": False}
        real_rid = room_data["data"]["room_id"]
        if room_data["data"].get("live_status") != 1: return {"streams": [], "isLive": False}
        play_resp = await request_with_retry("GET",
            f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8",
            headers=hdrs)
        play = play_resp.json()
        if play.get("code") != 0: return {"streams": [], "isLive": False}
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
        if not streams: return {"streams": [], "isLive": False}
        name, avatar = "B站主播", ""
        try:
            ir_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}", headers=hdrs)
            ir = ir_resp.json()
            ri = ir.get("data", {}).get("room_info", {})
            name = ri.get("uname") or name
            avatar = ri.get("face") or ""
        except Exception: pass
        return {"streams": streams[:4], "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[B站] 解析异常: {e}")
        return {"streams": [], "isLive": False}

# ==================== 抖音 ====================
async def parse_douyin(url):
    try:
        from streamget import DouyinLiveStream
        live = DouyinLiveStream()  # 不使用代理，默认直连
        data = await live.fetch_web_stream_data(url, process_data=True)
        stream_obj = await live.fetch_stream_url(data, "OD")
        raw = json.loads(stream_obj.to_json())
        streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
        if not streams:
            return {"streams": [], "isLive": False}
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        if not room_id.isdigit():
            try:
                resp = await request_with_retry("GET", url, headers={"User-Agent": UA})
                match = re.search(r'"room_id":"(\d+)"', resp.text)
                if match: room_id = match.group(1)
            except Exception: pass
        return {"streams": streams, "title": raw.get("anchor_name", "抖音主播"),
                "avatar": raw.get("avatar", ""), "roomId": room_id, "isLive": True}
    except Exception as e:
        print(f"[抖音] 解析异常: {e}")
        return {"streams": [], "isLive": False}

# ==================== Twitch ====================
async def parse_twitch(url):
    try:
        match = re.search(r"twitch\.tv/([^/?]+)", url)
        if not match: return {"streams": [], "isLive": False}
        channel = match.group(1)
        client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
        headers = {"Client-ID": client_id, "Content-Type": "application/json", "User-Agent": UA}
        gql_url = "https://gql.twitch.tv/gql"
        payload = [{"operationName": "PlaybackAccessToken", "variables": {"login": channel, "playerType": "site"},
                     "query": "query PlaybackAccessToken($login: String!, $playerType: String!) { streamPlaybackAccessToken(channelName: $login, params: { platform: \"web\", playerType: $playerType, playerBackend: \"mediaplayer\" }) { value signature } }"}]
        resp = await request_with_proxy_group("POST", gql_url, proxy_list=EXTERNAL_PROXY_URLS, json=payload, headers=headers)
        if resp.status_code != 200: return {"streams": [], "isLive": False}
        data = resp.json()
        token = sig = None
        if isinstance(data, list) and len(data) > 0:
            t = data[0].get("data", {}).get("streamPlaybackAccessToken")
            if t: token, sig = t.get("value"), t.get("signature")
        if not token or not sig: return {"streams": [], "isLive": False}
        m3u8_url = f"https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8?sig={sig}&token={quote(token, safe='')}&allow_source=true&allow_audio_only=true"
        usher_resp = await request_with_proxy_group("GET", m3u8_url, proxy_list=EXTERNAL_PROXY_URLS,
                                                     headers={"User-Agent": UA, "Referer": "https://player.twitch.tv"})
        if usher_resp.status_code != 200: return {"streams": [], "isLive": False}
        if "#EXT-X-STREAM-INF" not in usher_resp.text: return {"streams": [], "isLive": False}
        streams, lines = [], usher_resp.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                name = "source"
                if "RESOLUTION=" in line: name = line.split("RESOLUTION=")[1].split(",")[0].replace("x", "p")
                if i+1 < len(lines):
                    sub_url = lines[i+1].strip()
                    if not sub_url.startswith("http"):
                        from urllib.parse import urljoin
                        sub_url = urljoin(m3u8_url, sub_url)
                    streams.append({"cdn": f"Twitch-{name}", "url": sub_url, "type": "m3u8"})
        streams.sort(key=lambda s: (0 if "source" in s["cdn"] else 1, s["cdn"]))
        if not streams: return {"streams": [], "isLive": False}
        return {"streams": streams, "title": channel, "avatar": "", "channelName": channel, "isLive": True}
    except Exception as e:
        print(f"[Twitch] 解析异常: {e}")
        return {"streams": [], "isLive": False}

# ==================== SOOP ====================
async def parse_soop(url):
    try:
        parts = url.rstrip('/').split('/')
        # play.sooplive.com/{username} 或 play.sooplive.com/{username}/{broadcast_no}
        # bj_id 始终是主播用户名，固定在 parts[3]，不能用 isdigit() 覆盖为直播号
        bj_id = parts[3].split('?')[0] if len(parts) > 3 else parts[-1].split('?')[0]

        headers_pc = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://play.sooplive.com',
            'referer': 'https://play.sooplive.com',
        }

        # 1. 获取主播昵称
        nick_api = f'https://st.sooplive.com/api/get_station_status.php?szBjId={bj_id}'
        nick_resp = await request_with_proxy_group("GET", nick_api, proxy_list=EXTERNAL_PROXY_URLS, headers=headers_pc)
        if nick_resp.status_code != 200:
            return {"streams": [], "isLive": False}
        nick_data = nick_resp.json()
        nickname = nick_data.get('DATA', {}).get('user_nick', f'BJ-{bj_id}')

        # 2. 获取直播状态、broad_no、title
        live_api = f'https://live.sooplive.com/afreeca/player_live_api.php?bjid={bj_id}'
        live_data_form = {
            'bid': bj_id,
            'bno': '',
            'type': '',
            'pwd': '',
            'player_type': 'html5',
            'stream_type': 'common',
            'quality': 'master',
            'mode': 'landing',
            'from_api': '0',
            'is_revive': 'false',
        }
        live_resp = await request_with_proxy_group("POST", live_api, proxy_list=EXTERNAL_PROXY_URLS, headers=headers_pc, data=live_data_form)
        if live_resp.status_code != 200:
            return {"streams": [], "isLive": False}
        live_json = live_resp.json()
        channel = live_json.get('CHANNEL', {})
        result_code = channel.get('RESULT', -1)
        if result_code not in [0, 1]:
            return {"streams": [], "isLive": False}
        broad_no = channel.get('BNO', '')
        title = channel.get('TITLE', 'SOOP直播')
        if not broad_no:
            return {"streams": [], "isLive": False}

        # 3. 动态时间戳获取CDN View URL
        ts = time.time()
        cdn_params = {
            'return_type': 'gcp_cdn',
            'use_cors': 'false',
            'cors_origin_url': 'play.sooplive.com',
            'broad_key': f'{broad_no}-common-master-hls',
            'time': str(ts),
        }
        cdn_url = 'http://livestream-manager.sooplive.com/broad_stream_assign.html'
        cdn_resp = await request_with_proxy_group("GET", cdn_url, proxy_list=EXTERNAL_PROXY_URLS, headers=headers_pc, params=cdn_params)
        if cdn_resp.status_code != 200:
            return {"streams": [], "isLive": False}
        cdn_json = cdn_resp.json()
        view_url = cdn_json.get('view_url')
        if not view_url:
            return {"streams": [], "isLive": False}

        # 4. 获取 AID 鉴权
        aid_form = live_data_form.copy()
        aid_form['type'] = 'aid'
        aid_resp = await request_with_proxy_group("POST", live_api, proxy_list=EXTERNAL_PROXY_URLS, headers=headers_pc, data=aid_form)
        if aid_resp.status_code != 200:
            return {"streams": [], "isLive": False}
        aid_json = aid_resp.json()
        aid = aid_json.get('CHANNEL', {}).get('AID', '')
        if not aid:
            return {"streams": [], "isLive": False}

        # 5. 拼接最终 m3u8
        m3u8_url = f'{view_url}?aid={aid}'

        # 6. 请求 master m3u8，解析多画质子流
        streams = []
        try:
            master_resp = await request_with_proxy_group(
                "GET", m3u8_url, proxy_list=EXTERNAL_PROXY_URLS,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://play.sooplive.com"
                }
            )
            if master_resp.status_code == 200:
                lines = master_resp.text.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        # 默认名称
                        name = "Source"
                        # 优先用分辨率
                        res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                        if res_match:
                            name = res_match.group(1).split('x')[1] + 'p'  # 如 720p
                        else:
                            # 次选带宽
                            bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                            if bw_match:
                                kbps = int(int(bw_match.group(1)) / 1000)
                                name = f"{kbps}k"
                        # 下一行是子流 URL
                        if i + 1 < len(lines):
                            sub_url = lines[i + 1].strip()
                            if not sub_url:
                                continue
                            # 补全相对路径
                            if not sub_url.startswith("http"):
                                from urllib.parse import urljoin
                                sub_url = urljoin(m3u8_url, sub_url)
                            streams.append({
                                "cdn": f"SOOP-{name}",
                                "url": sub_url,
                                "type": "m3u8"
                            })
                # 按带宽从高到低排序（画质降序）
                if streams:
                    streams.sort(key=lambda s: float(s['cdn'].replace('SOOP-','').replace('p','').replace('k','000')), reverse=True)
        except Exception as e:
            print(f"[SOOP] 解析多画质失败，回退到单一源: {e}")

        # 如果解析不出子流，退回原来的单源
        if not streams:
            streams = [{"cdn": "SOOP-Source", "url": m3u8_url, "type": "m3u8"}]

        return {"streams": streams, "title": f"{nickname}-{bj_id}", "avatar": "", "isLive": True}

# ==================== PandaTV ====================
async def parse_panda(url):
    try:
        from streamget.platforms.pandatv.live_stream import PandaTvLiveStream
        live = PandaTvLiveStream()
        data = await live.fetch_web_stream_data(url, process_data=True)
        stream_obj = await live.fetch_stream_url(data, "OD")
        raw = json.loads(stream_obj.to_json())
        streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
        return {"streams": streams, "title": raw.get("anchor_name", "PandaTV主播"), "avatar": raw.get("avatar", ""), "isLive": raw.get("is_live", False)}
    except Exception as e:
        print(f"[PandaTV] streamget 解析失败: {e}, 回退手动解析")
        return await parse_panda_manual(url)

async def parse_panda_manual(url):
    try:
        user_id = url.split('?')[0].rstrip('/').split('/')[-1]
        headers = {'origin': 'https://www.pandalive.co.kr', 'referer': 'https://www.pandalive.co.kr/', 'user-agent': UA}
        info_url = 'https://api.pandalive.co.kr/v1/member/bj'
        resp = await request_with_proxy_group("POST", info_url, proxy_list=EXTERNAL_PROXY_URLS, headers=headers, data={'userId': user_id, 'info': 'media fanGrade'})
        if resp.status_code != 200: return {"streams": [], "isLive": False}
        info_json = resp.json()
        if 'bjInfo' not in info_json: return {"streams": [], "isLive": False}
        anchor_name = info_json['bjInfo']['nick']
        if 'media' not in info_json: return {"streams": [], "isLive": False}
        play_url = 'https://api.pandalive.co.kr/v1/live/play'
        resp2 = await request_with_proxy_group("POST", play_url, proxy_list=EXTERNAL_PROXY_URLS, headers=headers, data={'action': 'watch', 'userId': user_id, 'password': '', 'shareLinkType': ''})
        if resp2.status_code != 200: return {"streams": [], "isLive": False}
        play_json = resp2.json()
        if 'PlayList' not in play_json or 'hls' not in play_json['PlayList']: return {"streams": [], "isLive": False}
        real_m3u8 = play_json['PlayList']['hls'][0]['url']
        streams = [{"cdn": "PandaTV-Source", "url": real_m3u8, "type": "m3u8"}]
        return {"streams": streams, "title": f"{anchor_name}-{user_id}", "avatar": "", "isLive": True}
    except Exception as e:
        print(f"[PandaTV] 手动解析异常: {e}")
        return {"streams": [], "isLive": False}

@app.get("/api/parse")
async def api_parse(url: str = Query(...)):
    try:
        if "huya.com" in url: return await parse_huya(url)
        if "douyu.com" in url: return await parse_douyu(url)
        if "bilibili.com" in url: return await parse_bilibili(url)
        if "douyin.com" in url: return await parse_douyin(url)
        if "twitch.tv" in url: return await parse_twitch(url)
        if "sooplive.com" in url: return await parse_soop(url)
        if "pandalive.co.kr" in url: return await parse_panda(url)
        raise HTTPException(400, "不支持的平台")
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

# ==================== 抖音弹幕代理 ====================
@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    await websocket.accept()
    print(f"[WS] 前端连接抖音弹幕代理: room_id={room_id}")

    go_ws_url = f"ws://localhost:1088/ws/{room_id}"
    go_ws = None
    client_task = None
    go_task = None

    try:
        go_ws = await websockets.connect(go_ws_url, ping_interval=None)
        print(f"[WS] 已连接 Go 服务: {go_ws_url}")

        async def forward_to_go():
            try:
                while True:
                    data = await websocket.receive_text()
                    if data == 'ping':
                        await websocket.send_text('pong')
                        continue
                    if go_ws and go_ws.state.name == 'OPEN':
                        await go_ws.send(data)
            except WebSocketDisconnect:
                print("[WS] 前端断开")
            except Exception as e:
                print(f"[WS] forward_to_go error: {e}")

        async def forward_to_frontend():
            try:
                while True:
                    data = await go_ws.recv()
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await websocket.send_text(data)
            except Exception as e:
                print(f"[WS] forward_to_frontend error: {e}")

        client_task = asyncio.create_task(forward_to_go())
        go_task = asyncio.create_task(forward_to_frontend())

        done, pending = await asyncio.wait(
            [client_task, go_task],
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        print(f"[WS] 无法连接 Go 服务: {e}")
    finally:
        if go_ws:
            try:
                await go_ws.close()
            except Exception:
                pass
        try:
            await websocket.close(code=1011)
        except Exception:
            pass

# ==================== Twitch 弹幕代理 ====================
@app.websocket("/ws/twitch/{channel_name}")
async def websocket_twitch_danmaku(websocket: WebSocket, channel_name: str):
    await websocket.accept()
    print(f"[WS] 前端连接 Twitch 弹幕: {channel_name}")
    stop_event = threading.Event()
    message_queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    def on_message(ws, msg):
        if msg.startswith("PING"): ws.send("PONG :tmi.twitch.tv"); return
        if msg.startswith("PONG"): return
        match = re.match(r":(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.*)", msg)
        if match:
            asyncio.run_coroutine_threadsafe(message_queue.put({"type": "chat", "nick": match.group(1), "content": match.group(2)}), loop)
    def on_error(ws, error): print(f"[Twitch IRC] 错误: {error}")
    def on_close(ws, code, msg): print("[Twitch IRC] 连接关闭")
    def run_irc():
        ws = websocket.WebSocketApp("wss://irc-ws.chat.twitch.tv:443", on_message=on_message, on_error=on_error, on_close=on_close)
        ws.on_open = lambda ws: (ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands"), ws.send("PASS SCHMOOPIIE"), ws.send("NICK justinfan12345"), ws.send(f"JOIN #{channel_name.lower()}"))
        ws.run_forever()
    task = loop.run_in_executor(None, run_irc)
    async def send_worker():
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError: continue
            except Exception: break
    send_task = asyncio.create_task(send_worker())
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping": await websocket.send_text("pong")
    except WebSocketDisconnect:
        print(f"[WS] 前端断开 Twitch: {channel_name}")
    finally:
        stop_event.set()
        send_task.cancel()
        try: await send_task
        except: pass
        task.cancel()

@app.get("/")
def root(): return {"status": "ok", "message": "多平台直播解析 API"}

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check(): return {"status": "alive"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
