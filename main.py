import json
import re
import httpx
import asyncio
import threading
from fastapi import FastAPI, Query, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from streamget import HuyaLiveStream, BilibiliLiveStream, DouyinLiveStream
import requests
import time
import hashlib
import base64
import random
from urllib.parse import unquote

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# ==================== 全局变量：存储活跃的抖音弹幕连接 ====================
active_douyin_connections = {}  # {room_id: {"task": asyncio.Task, "ws_list": [WebSocket], "stop_event": threading.Event}}


# ==================== /api/proxy ====================
@app.api_route("/api/proxy", methods=["GET", "POST"])
async def api_proxy(request: Request,
                    url: str = Query(...),
                    referer: str = Query(""),
                    ua: str = Query(""),
                    cookie: str = Query("")):
    ALLOWED = ["douyu.com", "huya.com", "bilibili.com", "bilivideo.com",
               "douyucdn.cn", "douyin.com", "live.bilibili.com"]
    if not any(d in url for d in ALLOWED):
        raise HTTPException(403, "domain not allowed")

    body = await request.body() if request.method == "POST" else None
    headers = {
        "User-Agent": ua or UA,
        "Referer": referer or "",
        "Cookie": cookie,
    }
    if request.method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(request.method, url, headers=headers, content=body)

    out_headers = {"Access-Control-Allow-Origin": "*"}
    ct = resp.headers.get("content-type", "application/json")
    out_headers["Content-Type"] = ct
    return StreamingResponse(iter([resp.content]), status_code=resp.status_code, headers=out_headers)


# ==================== 工具 ====================
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
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://m.huya.com/{room_id}",
                            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/100 Mobile", "Referer": "https://www.huya.com/"})
            html = r.text
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
    ws_secret_raw = "_".join([p, "0", stream_name, seqid, ws_time])
    ws_secret = hashlib.md5(ws_secret_raw.encode()).hexdigest()
    params["wsSecret"] = ws_secret
    params["seqid"] = seqid
    params["u"] = "0"
    return "&".join(f"{k}={v}" for k, v in params.items())


async def parse_huya(url):
    room_id = url.rstrip("/").split("/")[-1].split("?")[0]
    CDN_NAMES = {"AL": "阿里云", "TX": "腾讯云", "HW": "华为云", "WS": "网宿", "BD": "百度云"}
    CDN_ORDER = {"TX": 0, "AL": 1, "HW": 2, "WS": 3, "BD": 4}

    async with httpx.AsyncClient(timeout=15) as c:
        api_r = await c.get(
            f"https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={room_id}",
            headers={"User-Agent": UA, "Referer": "https://www.huya.com/"}
        )
        data = api_r.json()

    if data.get("status") != 200:
        raise ValueError(f"虎牙 API 错误: {data.get('message', data.get('status'))}")

    live = data["data"]
    if live.get("realLiveStatus") != "ON":
        raise ValueError("虎牙：未开播")

    cdn_list = live.get("stream", {}).get("baseSteamInfoList", [])
    if not cdn_list:
        raise ValueError("虎牙：未找到流信息")

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
        raise ValueError("虎牙：流地址构建失败")

    profile = live.get("profileRoom", {})
    room_info = live.get("roomInfo", {})
    live_data = live.get("liveData", {})
    anchor = live.get("anchor", {})

    anchor_name = (
        profile.get("nick") or
        room_info.get("nick") or
        live_data.get("nick") or
        anchor.get("nick") or
        None
    )

    if not anchor_name:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                mob_html = await c.get(
                    f"https://m.huya.com/{room_id}",
                    headers={"User-Agent": "Mozilla/5.0 (Linux; Android 11) Chrome/100 Mobile"}
                )
                title_match = re.search(r'<title>(.*?)</title>', mob_html.text)
                if title_match:
                    raw_title = title_match.group(1)
                    anchor_name = raw_title.split("_")[0].strip()
                else:
                    nick_match = re.search(r'"nick":"([^"]+)"', mob_html.text)
                    if nick_match:
                        anchor_name = nick_match.group(1)
        except Exception:
            pass

    if not anchor_name:
        anchor_name = "虎牙主播"

    avatar = (
        profile.get("avatar") or
        room_info.get("avatar") or
        live_data.get("avatar") or
        anchor.get("avatar") or
        ""
    )

    danmaku = await fetch_huya_danmaku_params(room_id)
    return {"streams": streams, "title": anchor_name, "avatar": avatar, "danmaku": danmaku}


# ==================== 斗鱼 ====================
async def parse_douyu(url):
    room_id = url.rstrip("/").split("/")[-1].split("?")[0]
    hdrs = {"User-Agent": UA, "Referer": f"https://www.douyu.com/{room_id}"}
    async with httpx.AsyncClient(timeout=15, headers=hdrs) as c:
        info = (await c.get(f"https://www.douyu.com/betard/{room_id}")).json()
        room = info.get("room")
        if not room:
            raise ValueError("斗鱼：房间不存在")
        if room.get("show_status") != 1 or room.get("videoLoop") == 1:
            raise ValueError("斗鱼：未开播")
        real_id = str(room["room_id"])
        enc = (await c.get(f"https://www.douyu.com/swf_api/homeH5Enc?rids={real_id}")).json()
        crptext = enc.get("data", {}).get(f"room{real_id}")
        if not crptext:
            raise ValueError("斗鱼：未获取到签名代码")

    raw_av = room.get("room_icon") or room.get("avatar") or ""
    if isinstance(raw_av, dict):
        raw_av = raw_av.get("big") or raw_av.get("middle") or raw_av.get("small") or ""

    return {
        "client": True,
        "crptext": crptext,
        "roomId": real_id,
        "anchorName": room.get("nickname") or room.get("owner_name") or "斗鱼主播",
        "avatar": raw_av,
        "isLive": True
    }


# ==================== B站 ====================
async def parse_bilibili(url):
    rid = url.rstrip("/").split("/")[-1].split("?")[0]
    hdrs = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
    async with httpx.AsyncClient(timeout=15, headers=hdrs) as c:
        room_resp = (await c.get(f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}")).json()
        if room_resp.get("code") != 0:
            raise ValueError(f"B站房间信息失败: {room_resp.get('message')}")
        real_rid = room_resp["data"]["room_id"]
        if room_resp["data"].get("live_status") != 1:
            raise ValueError("B站：未开播")

        play = (await c.get(
            f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
            f"?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8"
        )).json()
        if play.get("code") != 0:
            raise ValueError(f"B站拉流失败: {play.get('message')}")

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
            raise ValueError("B站：流地址提取失败")

        name, avatar = "B站主播", ""
        try:
            ir = (await c.get(f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}")).json()
            ri = ir.get("data", {}).get("room_info", {})
            name = ri.get("uname") or ri.get("owner_name") or name
            avatar = ri.get("face") or avatar
        except Exception:
            pass

        return {"streams": streams[:4], "title": name, "avatar": avatar}


# ==================== 抖音流解析 ====================
async def parse_douyin(url):
    from streamget import DouyinLiveStream
    live = DouyinLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"抖音未获取到流: {raw}")

    # 提取 room_id 用于弹幕
    room_id = url.rstrip("/").split("/")[-1].split("?")[0]
    if not room_id.isdigit():
        # 如果 URL 中是短码，从 HTML 中解析真实 room_id
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.get(url, headers={"User-Agent": UA})
                html = resp.text
                match = re.search(r'"room_id":"(\d+)"', html)
                if match:
                    room_id = match.group(1)
        except Exception:
            pass

    return {
        "streams": streams,
        "title": raw.get("anchor_name", "抖音主播"),
        "avatar": raw.get("avatar", ""),
        "roomId": room_id
    }


# ==================== 抖音弹幕采集器（基于 DouyinLiveWebFetcher） ====================

def fetch_douyin_room_info(room_id):
    """获取直播间基本信息（用于弹幕连接前的验证）"""
    try:
        url = f"https://live.douyin.com/{room_id}"
        headers = {"User-Agent": UA}
        resp = requests.get(url, headers=headers, timeout=10)
        html = resp.text

        # 提取 ttwid
        ttwid = ""
        for cookie in resp.cookies:
            if cookie.name == "ttwid":
                ttwid = cookie.value
                break

        # 提取 web_rid（真实房间ID）
        match = re.search(r'"room_id":"(\d+)"', html)
        web_rid = match.group(1) if match else room_id

        # 提取主播名和头像
        name_match = re.search(r'"nickname":"([^"]+)"', html)
        avatar_match = re.search(r'"avatar_thumb":\{"url_list":\["([^"]+)"\]', html)
        if not avatar_match:
            avatar_match = re.search(r'"avatar":"([^"]+)"', html)

        return {
            "web_rid": web_rid,
            "ttwid": ttwid,
            "anchor_name": name_match.group(1) if name_match else "抖音主播",
            "avatar": avatar_match.group(1).replace("\\u002F", "/") if avatar_match else ""
        }
    except Exception as e:
        return {"web_rid": room_id, "ttwid": "", "anchor_name": "抖音主播", "avatar": "", "error": str(e)}


def generate_douyin_signature(room_id, ttwid):
    """生成抖音弹幕 WebSocket 所需的签名"""
    # 简化版签名生成（实际需要调用 sign.js）
    # 对于研究用途，可以用 DouyinLiveWebFetcher 的 ac_signature 模块
    try:
        import hashlib
        timestamp = int(time.time())
        raw = f"{room_id}{ttwid}{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()
    except Exception:
        return ""


async def douyin_danmaku_collector(room_id, ttwid, stop_event, message_callback):
    """
    抖音弹幕采集器
    - 使用 websocket-client 连接到抖音弹幕服务器
    - 解析 protobuf 消息并调用 message_callback
    """
    import websocket
    import threading
    from datetime import datetime

    try:
        # 尝试导入 protobuf 生成的模块（如果存在）
        try:
            from protobuf import douyin_pb2
        except ImportError:
            douyin_pb2 = None

        # 构建 WebSocket URL
        ws_url = f"wss://webcast3-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web&version_code=180800&webcast_sdk_version=1.0.14&update_version_code=1.0.14&compress=gzip&internal_ext=internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:0|first_req_ms:{int(time.time()*1000)}|fetch_time:{int(time.time()*1000)}|seq:1|wss_info:0-0-0-0&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&debug=false&endpoint=live&support_wrds=1&im_path=/webcast/im/fetch/&user_unique_id=&device_platform=web&cookie_enabled=true&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome&browser_version=120.0.0.0&browser_online=true&tz_name=Asia/Shanghai&identity=audience&room_id={room_id}&heartbeatDuration=0&signature="

        ws = websocket.WebSocketApp(
            ws_url,
            on_open=lambda ws: on_open(ws, room_id),
            on_message=lambda ws, msg: on_message(ws, msg, message_callback, douyin_pb2),
            on_error=lambda ws, err: print(f"[抖音弹幕] 错误: {err}"),
            on_close=lambda ws, code, msg: print(f"[抖音弹幕] 连接关闭: {code} {msg}")
        )

        def on_open(ws, room_id):
            print(f"[抖音弹幕] 已连接房间 {room_id}")
            # 发送心跳
            def heartbeat():
                while not stop_event.is_set():
                    time.sleep(10)
                    try:
                        if ws.sock and ws.sock.connected:
                            ws.send(b"", opcode=websocket.ABNF.OPCODE_PING)
                    except Exception:
                        break
            threading.Thread(target=heartbeat, daemon=True).start()

        def on_message(ws, message, callback, pb2_module):
            if stop_event.is_set():
                return

            try:
                # 解析 PushFrame
                if pb2_module:
                    push_frame = pb2_module.PushFrame()
                    push_frame.ParseFromString(message)
                    payload = push_frame.payload

                    # 解析 Response
                    response = pb2_module.Response()
                    response.ParseFromString(payload)

                    for msg in response.messages_list:
                        method = msg.method
                        payload = msg.payload

                        # 解析具体消息类型
                        if method == "WebcastChatMessage":
                            chat = pb2_module.ChatMessage()
                            chat.ParseFromString(payload)
                            nick = chat.user.nickname
                            content = chat.content
                            callback({
                                "type": "chat",
                                "nick": nick,
                                "content": content,
                                "time": int(time.time() * 1000)
                            })
                        elif method == "WebcastGiftMessage":
                            gift = pb2_module.GiftMessage()
                            gift.ParseFromString(payload)
                            nick = gift.user.nickname
                            gift_name = gift.gift.name
                            callback({
                                "type": "gift",
                                "nick": nick,
                                "gift": gift_name,
                                "count": gift.repeat_count,
                                "time": int(time.time() * 1000)
                            })
                        elif method == "WebcastMemberMessage":
                            member = pb2_module.MemberMessage()
                            member.ParseFromString(payload)
                            nick = member.user.nickname
                            callback({
                                "type": "enter",
                                "nick": nick,
                                "action": "进入了直播间",
                                "time": int(time.time() * 1000)
                            })
                        elif method == "WebcastLikeMessage":
                            like = pb2_module.LikeMessage()
                            like.ParseFromString(payload)
                            nick = like.user.nickname
                            callback({
                                "type": "like",
                                "nick": nick,
                                "count": like.count,
                                "time": int(time.time() * 1000)
                            })
            except Exception as e:
                # 如果 protobuf 解析失败，尝试简单的文本解析（降级）
                try:
                    import json
                    data = json.loads(message)
                    if "common" in data and "content" in data:
                        callback({
                            "type": "chat",
                            "nick": data.get("user", {}).get("nickname", "匿名"),
                            "content": data.get("content", ""),
                            "time": int(time.time() * 1000)
                        })
                except Exception:
                    pass

        # 运行 WebSocket
        ws.run_forever()

    except Exception as e:
        print(f"[抖音弹幕] 采集器异常: {e}")
        # 重试机制
        if not stop_event.is_set():
            await asyncio.sleep(3)
            await douyin_danmaku_collector(room_id, ttwid, stop_event, message_callback)


# ==================== WebSocket 路由：前端连接弹幕 ====================

@app.websocket("/ws/douyin/{room_id}")
async def websocket_douyin_danmaku(websocket: WebSocket, room_id: str):
    """前端通过此 WebSocket 连接接收抖音弹幕"""
    await websocket.accept()
    print(f"[WS] 前端连接抖音弹幕: {room_id}")

    # 定义回调：收到弹幕后推送给前端
    def message_callback(msg):
        try:
            # 需要在主事件循环中发送
            asyncio.create_task(send_to_client(msg))
        except Exception:
            pass

    async def send_to_client(msg):
        try:
            await websocket.send_json(msg)
        except Exception:
            pass

    # 获取房间信息
    info = fetch_douyin_room_info(room_id)
    web_rid = info.get("web_rid", room_id)
    ttwid = info.get("ttwid", "")

    # 创建停止事件
    stop_event = threading.Event()

    # 启动采集任务
    task = asyncio.create_task(
        douyin_danmaku_collector(web_rid, ttwid, stop_event, message_callback)
    )

    # 存储连接信息
    if room_id not in active_douyin_connections:
        active_douyin_connections[room_id] = {"task": task, "ws_list": [], "stop_event": stop_event}
    active_douyin_connections[room_id]["ws_list"].append(websocket)

    try:
        while True:
            # 保持连接，接收前端消息（如心跳）
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        print(f"[WS] 前端断开抖音弹幕: {room_id}")
    finally:
        # 清理
        if room_id in active_douyin_connections:
            conn_info = active_douyin_connections[room_id]
            if websocket in conn_info["ws_list"]:
                conn_info["ws_list"].remove(websocket)
            # 如果没有前端连接了，停止采集
            if not conn_info["ws_list"]:
                conn_info["stop_event"].set()
                conn_info["task"].cancel()
                del active_douyin_connections[room_id]


# ==================== /api/parse 路由 ====================

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
