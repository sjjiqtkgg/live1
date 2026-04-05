import json
import re
import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from streamget import HuyaLiveStream, BilibiliLiveStream, DouyinLiveStream

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


# ==================== /api/proxy  (前端借此绕过 CORS) ====================
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


# ==================== 虎牙 (streamget + 多 CDN) ====================
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

async def parse_huya(url):
    room_id = url.rstrip("/").split("/")[-1].split("?")[0]
    live = HuyaLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"虎牙未获取到流: {raw}")
    danmaku = await fetch_huya_danmaku_params(room_id)
    return {"streams": streams, "title": raw.get("anchor_name", "虎牙主播"),
            "avatar": raw.get("avatar", ""), "danmaku": danmaku}


# ==================== 斗鱼 (返回 crptext 给前端签名，前端多 CDN) ====================
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

    # avatar 字段可能是字符串或 {"big":..,"middle":..,"small":..} 对象，统一转成字符串
    raw_avatar = room.get("room_icon") or room.get("avatar") or ""
    if isinstance(raw_avatar, dict):
        avatar_str = raw_avatar.get("big") or raw_avatar.get("middle") or raw_avatar.get("small") or ""
    else:
        avatar_str = raw_avatar

    # 返回 client:true，让前端用 douyuSignAndPlay 签名并拿多 CDN
    return {
        "client": True,
        "crptext": crptext,
        "roomId": real_id,
        "anchorName": room.get("nickname") or room.get("owner_name") or "斗鱼主播",
        "avatar": avatar_str,
        "isLive": True
    }


# ==================== B站 (直接调官方 API) ====================
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


# ==================== 抖音 (streamget) ====================
async def parse_douyin(url):
    from streamget import DouyinLiveStream
    live = DouyinLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"抖音未获取到流: {raw}")
    return {"streams": streams, "title": raw.get("anchor_name", "抖音主播"), "avatar": raw.get("avatar", "")}


# ==================== 路由 ====================
@app.get("/api/parse")
async def api_parse(url: str = Query(...)):
    try:
        if "huya.com" in url:   return (await parse_huya(url))
        if "douyu.com" in url:  return (await parse_douyu(url))
        if "bilibili.com" in url: return (await parse_bilibili(url))
        if "douyin.com" in url: return (await parse_douyin(url))
        raise HTTPException(400, "不支持的平台")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/")
def root():
    return {"status": "ok", "message": "多平台直播解析 API"}
