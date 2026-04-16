import json
import re
import os
import httpx
import asyncio
import threading
import time
import hashlib
import base64
import random
import execjs
import websocket
from fastapi import FastAPI, Query, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from urllib.parse import unquote, urlparse, parse_qs
from protobuf import douyin

try:
    from python_socks.sync import Proxy
    from python_socks import ProxyType
    SOCKS_SUPPORT = True
except ImportError:
    SOCKS_SUPPORT = False
    print("[警告] python_socks 未安装，WebSocket 将不使用代理")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

PROXY_LIST_STR = os.getenv("PROXY_LIST", "socks5://43.139.29.27:1111")
PROXY_URLS = [p.strip() for p in PROXY_LIST_STR.split(",") if p.strip()]
print(f"[代理] 共加载 {len(PROXY_URLS)} 个代理: {PROXY_URLS}")


async def request_with_retry(method: str, url: str, **kwargs):
    last_error = None
    timeout = kwargs.pop("timeout", 15)
    for proxy in PROXY_URLS:
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
                resp = await client.request(method, url, **kwargs)
                return resp
        except Exception as e:
            last_error = e
            print(f"[请求重试] 代理 {proxy} 失败: {e}，尝试下一个...")
    raise last_error or Exception("所有代理均失败")


@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request, url: str = Query(...), referer: str = Query(""), ua: str = Query(""), cookie: str = Query("")):
    ALLOWED = ["douyu.com", "huya.com", "bilibili.com", "bilivideo.com", "douyucdn.cn", "douyin.com", "live.bilibili.com"]
    if not any(d in url for d in ALLOWED):
        raise HTTPException(403, "domain not allowed")
    body = await request.body() if request.method == "POST" else None
    headers = {"User-Agent": ua or UA, "Referer": referer or "", "Cookie": cookie}
    if request.method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    resp = await request_with_retry(request.method, url, headers=headers, content=body)
    out_headers = {"Access-Control-Allow-Origin": "*", "Content-Type": resp.headers.get("content-type", "application/json")}
    return StreamingResponse(iter([resp.content]), status_code=resp.status_code, headers=out_headers)


def build_streams(flv, m3u8):
    s = []
    if flv and flv.startswith("http"):
        s.append({"cdn": "FLV", "url": flv, "type": "flv"})
    if m3u8 and m3u8.startswith("http"):
        s.append({"cdn": "HLS", "url": m3u8, "type": "m3u8"})
    return s


async def fetch_huya_danmaku_params(room_id):
    try:
        resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}",
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/100 Mobile", "Referer": "https://www.huya.com/"})
        html = resp.text
        ayyuid  = int((re.search(r'"lYyid":(\d+)', html) or re.search(r'ayyuid:\s*["\']?(\d+)', html) or [None, 0])[1])
        top_sid = int((re.search(r'"lChannelId":(\d+)', html) or [None, 0])[1])
        sub_sid = int((re.search(r'"lSubChannelId":(\d+)', html) or [None, 0])[1])
        return {"platform": "huya", "ayyuid": ayyuid, "topSid": top_sid, "subSid": sub_sid}
    except Exception:
        return {}


def huya_build_anticode(raw_anti: str, stream_name: str) -> str:
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
        streams = []
        seen_cdns = set()
        for s in cdn_list:
            cdn_type = s.get("sCdnType", "")
            if cdn_type in seen_cdns:
                continue
            flv_url = s.get("sFlvUrl", "")
            stream_name = s.get("sStreamName", "")
            anti_code = s.get("sFlvAntiCode", "")
            suffix = s.get("sFlvUrlSuffix", "flv")
            if not (flv_url and stream_name and anti_code):
                continue
            built = huya_build_anticode(anti_code, stream_name)
            full_url = f"{flv_url}/{stream_name}.{suffix}?{built}"
            label = CDN_NAMES.get(cdn_type, cdn_type or "CDN")
            streams.append({"cdn": label, "url": full_url.replace("http://", "https://"), "type": "flv"})
            seen_cdns.add(cdn_type)
        if not streams:
            return {"streams": [], "isLive": False}
        profile = live.get("profileRoom", {})
        room_info = live.get("roomInfo", {})
        live_data = live.get("liveData", {})
        anchor = live.get("anchor", {})
        anchor_name = profile.get("nick") or room_info.get("nick") or live_data.get("nick") or anchor.get("nick")
        if not anchor_name:
            try:
                mob_resp = await request_with_retry("GET", f"https://m.huya.com/{room_id}",
                    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/100 Mobile"})
                mob_html = mob_resp.text
                title_match = re.search(r'<title>(.*?)</title>', mob_html)
                if title_match:
                    anchor_name = title_match.group(1).split("_")[0].strip()
                else:
                    nick_match = re.search(r'"nick":"([^"]+)"', mob_html)
                    if nick_match:
                        anchor_name = nick_match.group(1)
            except Exception:
                pass
        anchor_name = anchor_name or "虎牙主播"
        avatar = profile.get("avatar") or room_info.get("avatar") or live_data.get("avatar") or anchor.get("avatar") or ""
        danmaku = await fetch_huya_danmaku_params(room_id)
        return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku, "isLive": True}
    except Exception as e:
        print(f"[虎牙] 解析异常: {e}")
        return {"streams": [], "isLive": False}


async def parse_douyu(url):
    try:
        room_id = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": f"https://www.douyu.com/{room_id}"}
        info_resp = await request_with_retry("GET", f"https://www.douyu.com/betard/{room_id}", headers=hdrs)
        info = info_resp.json()
        room = info.get("room")
        if not room:
            return {"streams": [], "isLive": False}
        if room.get("show_status") != 1 or room.get("videoLoop") == 1:
            return {"streams": [], "isLive": False}
        real_id = str(room["room_id"])
        enc_resp = await request_with_retry("GET", f"https://www.douyu.com/swf_api/homeH5Enc?rids={real_id}", headers=hdrs)
        enc = enc_resp.json()
        crptext = enc.get("data", {}).get(f"room{real_id}")
        if not crptext:
            return {"streams": [], "isLive": False}
        raw_av = room.get("room_icon") or room.get("avatar") or ""
        if isinstance(raw_av, dict):
            raw_av = raw_av.get("big") or raw_av.get("middle") or raw_av.get("small") or ""
        return {"client": True, "crptext": crptext, "roomId": real_id,
                "anchorName": room.get("nickname") or room.get("owner_name") or "斗鱼主播",
                "avatar": raw_av, "isLive": True}
    except Exception as e:
        print(f"[斗鱼] 解析异常: {e}")
        return {"streams": [], "isLive": False}


async def parse_bilibili(url):
    try:
        rid = url.rstrip("/").split("/")[-1].split("?")[0]
        hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
        room_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}", headers=hdrs)
        room_data = room_resp.json()
        if room_data.get("code") != 0:
            return {"streams": [], "isLive": False}
        real_rid = room_data["data"]["room_id"]
        if room_data["data"].get("live_status") != 1:
            return {"streams": [], "isLive": False}
        play_resp = await request_with_retry("GET",
            f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8",
            headers=hdrs)
        play = play_resp.json()
        if play.get("code") != 0:
            return {"streams": [], "isLive": False}
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
            return {"streams": [], "isLive": False}
        name, avatar = "B站主播", ""
        try:
            ir_resp = await request_with_retry("GET", f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}", headers=hdrs)
            ir = ir_resp.json()
            ri = ir.get("data", {}).get("room_info", {})
            name = ri.get("uname") or ri.get("owner_name") or name
            avatar = ri.get("face") or avatar
        except Exception:
            pass
        return {"streams": streams[:4], "title": name, "avatar": avatar, "isLive": True}
    except Exception as e:
        print(f"[B站] 解析异常: {e}")
        return {"streams": [], "isLive": False}


async def parse_douyin(url):
    try:
        from streamget import DouyinLiveStream
        live = DouyinLiveStream()
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
                if match:
                    room_id = match.group(1)
            except Exception:
                pass
        ttwid = ""
        try:
            resp = await request_with_retry("GET", url, headers={"User-Agent": UA})
            for cookie in resp.cookies:
                if cookie.name == "ttwid":
                    ttwid = cookie.value
                    break
        except Exception:
            pass
        return {"streams": streams, "title": raw.get("anchor_name", "抖音主播"),
                "avatar": raw.get("avatar", ""), "roomId": room_id, "ttwid": ttwid, "isLive": True}
    except Exception as e:
        print(f"[抖音] 解析异常: {e}")
        return {"streams": [], "isLive": False}


def get_douyin_signature(md5_str: str) -> str:
    try:
        with open("sign.js", "r", encoding="utf-8") as f:
            js_code = f.read()
        ctx = execjs.compile(js_code)
        return ctx.call("get_sign", md5_str)
    except Exception as e:
        print(f"[签名] 生成失败: {e}")
        return ""


def douyin_danmaku_collector_sync(room_id: str, ttwid: str, stop_event: threading.Event, callback):
    user_unique_id = str(random.randint(1000000000000000000, 9999999999999999999))
    base_ws_url = (
        f"wss://webcast3-ws-web-lq.douyin.com/webcast/im/push/v2/"
        f"?app_name=douyin_web&version_code=180800&webcast_sdk_version=1.0.14"
        f"&update_version_code=1.0.14&compress=gzip&internal_ext=internal_src:dim"
        f"|wss_push_room_id:{room_id}|wss_push_did:0|first_req_ms:{int(time.time()*1000)}"
        f"|fetch_time:{int(time.time()*1000)}|seq:1|wss_info:0-0-0-0"
        f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&debug=false"
        f"&endpoint=live&support_wrds=1&im_path=/webcast/im/fetch/&user_unique_id={user_unique_id}"
        f"&device_platform=web&cookie_enabled=true&screen_width=1920&screen_height=1080"
        f"&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome"
        f"&browser_version=120.0.0.0&browser_online=true&tz_name=Asia/Shanghai"
        f"&identity=audience&room_id={room_id}&heartbeatDuration=0"
    )
    params_order = ("live_id", "aid", "version_code", "webcast_sdk_version",
                    "room_id", "sub_room_id", "sub_channel_id", "did_rule",
                    "user_unique_id", "device_platform", "device_type", "ac", "identity")
    parsed = urlparse(base_ws_url)
    qs_dict = parse_qs(parsed.query)
    wss_maps = {k: v[0] if isinstance(v, list) else v for k, v in qs_dict.items()}
    param_str = ','.join(f"{p}={wss_maps.get(p, '')}" for p in params_order)
    md5_str = hashlib.md5(param_str.encode()).hexdigest()
    signature = get_douyin_signature(md5_str)
    ws_url = f"{base_ws_url}&signature={signature}"
    headers = {"User-Agent": UA, "Origin": "https://live.douyin.com"}
    if ttwid:
        headers["Cookie"] = f"ttwid={ttwid}"

    def on_open(ws):
        print(f"[抖音弹幕] 已连接房间 {room_id}")
        def heartbeat():
            while not stop_event.is_set():
                time.sleep(10)
                try:
                    if ws.sock and ws.sock.connected:
                        ws.send(b"", opcode=websocket.ABNF.OPCODE_PING)
                except Exception:
                    break
        threading.Thread(target=heartbeat, daemon=True).start()

    def on_message(ws, message):
        if stop_event.is_set():
            return
        try:
            push_frame = douyin.PushFrame().parse(message)
            response = douyin.Response().parse(push_frame.payload)
            for msg in response.messages_list:
                method = msg.method
                payload = msg.payload
                if method == "WebcastChatMessage":
                    try:
                        chat = douyin.ChatMessage().parse(payload)
                        if chat and chat.user and chat.content:
                            callback({"type": "chat", "nick": chat.user.nick_name or "匿名用户",
                                      "content": chat.content, "time": int(time.time() * 1000)})
                    except Exception as e:
                        print(f"[抖音弹幕] 解析聊天出错: {e}")
                elif method == "WebcastGiftMessage":
                    try:
                        gift = douyin.GiftMessage().parse(payload)
                        if gift and gift.user:
                            callback({"type": "gift", "nick": gift.user.nick_name or "匿名用户",
                                      "gift": gift.gift.name if gift.gift else "礼物",
                                      "count": gift.repeat_count, "time": int(time.time() * 1000)})
                    except Exception as e:
                        print(f"[抖音弹幕] 解析礼物出错: {e}")
                elif method == "WebcastLikeMessage":
                    try:
                        like = douyin.LikeMessage().parse(payload)
                        if like and like.user:
                            callback({"type": "like", "nick": like.user.nick_name or "匿名用户",
                                      "count": like.count, "time": int(time.time() * 1000)})
                    except Exception as e:
                        print(f"[抖音弹幕] 解析点赞出错: {e}")
        except Exception as e:
            print(f"[抖音弹幕] 解析顶层错误: {e}")

    def on_error(ws, error):
        print(f"[抖音弹幕] WebSocket 错误: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"[抖音弹幕] 连接关闭: {close_status_code} {close_msg}")
        if not stop_event.is_set():
            time.sleep(3)
            douyin_danmaku_collector_sync(room_id, ttwid, stop_event, callback)

    for idx, proxy_url in enumerate(PROXY_URLS):
        try:
            print(f"[抖音弹幕] 尝试代理 [{idx+1}/{len(PROXY_URLS)}]: {proxy_url}")
            if SOCKS_SUPPORT and proxy_url.startswith("socks5://"):
                proxy = Proxy.from_url(proxy_url)
                sock = proxy.connect(("webcast3-ws-web-lq.douyin.com", 443))
                ws = websocket.WebSocketApp(ws_url, header=headers, on_open=on_open,
                    on_message=on_message, on_error=on_error, on_close=on_close, sock=sock)
            elif proxy_url.startswith("http://") or proxy_url.startswith("https://"):
                proxy_parts = urlparse(proxy_url)
                ws = websocket.WebSocketApp(ws_url, header=headers, on_open=on_open,
                    on_message=on_message, on_error=on_error, on_close=on_close,
                    http_proxy_host=proxy_parts.hostname, http_proxy_port=proxy_parts.port,
                    http_proxy_auth=(proxy_parts.username, proxy_parts.password) if proxy_parts.username else None,
                    proxy_type="http")
            else:
                ws = websocket.WebSocketApp(ws_url, header=headers, on_open=on_open,
                    on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever()
            break
        except Exception as e:
            print(f"[抖音弹幕] 代理 {proxy_url} 失败: {e}，尝试下一个...")
            if idx == len(PROXY_URLS) - 1:
                print("[抖音弹幕] 所有代理均失败，停止重试")
                return
            time.sleep(1)


@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    await websocket.accept()
    print(f"[WS] 前端连接抖音弹幕: {room_id}")
    ttwid = ""
    try:
        resp = await request_with_retry("GET", f"https://live.douyin.com/{room_id}", headers={"User-Agent": UA})
        for cookie in resp.cookies:
            if cookie.name == "ttwid":
                ttwid = cookie.value
                break
    except Exception:
        pass
    if not ttwid:
        print("[ttwid] 获取失败，将尝试不带 ttwid 连接")
    stop_event = threading.Event()
    message_queue = asyncio.Queue()

    def callback(msg):
        asyncio.run_coroutine_threadsafe(message_queue.put(msg), loop)

    loop = asyncio.get_event_loop()
    task = loop.run_in_executor(None, lambda: douyin_danmaku_collector_sync(room_id, ttwid, stop_event, callback))

    async def send_worker():
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(message_queue.get(), timeout=1.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    send_task = asyncio.create_task(send_worker())
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        print(f"[WS] 前端断开抖音弹幕: {room_id}")
    finally:
        stop_event.set()
        send_task.cancel()
        try:
            await send_task
        except:
            pass
        task.cancel()


@app.get("/api/parse")
async def api_parse(url: str = Query(...)):
    try:
        if "huya.com" in url:
            return await parse_huya(url)
        if "douyu.com" in url:
            return await parse_douyu(url)
        if "bilibili.com" in url:
            return await parse_bilibili(url)
        if "douyin.com" in url:
            return await parse_douyin(url)
        raise HTTPException(400, "不支持的平台")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/")
def root():
    return {"status": "ok", "message": "多平台直播解析 API"}
