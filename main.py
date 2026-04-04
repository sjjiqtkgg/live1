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


# ==================== /api/proxy  (前端借此绕过 CORS，并强制修正斗鱼流地址) ====================
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

    # 如果是斗鱼的 getH5Play 接口，需要修正流地址为华为 CDN
    content_type = resp.headers.get("content-type", "")
    if "douyu.com/lapi/live/getH5Play" in url and request.method == "POST" and "application/json" in content_type:
        try:
            data = resp.json()
            # 修正 rtmp_url 和 rtmp_live
            if "data" in data and data.get("error") == 0:
                d = data["data"]
                if d.get("rtmp_url") and d.get("rtmp_live"):
                    # 强制替换域名为华为边缘节点
                    d["rtmp_url"] = re.sub(r'[^/]+\.douyucdn2\.cn', 'hwa.douyucdn2.cn', d["rtmp_url"])
                    # 修正 rtmp_live 中的 fcdn 参数
                    d["rtmp_live"] = re.sub(r'fcdn=[^&]+', 'fcdn=hw', d["rtmp_live"])
                    # 同时修正 hls_url 如果存在
                    if d.get("hls_url"):
                        d["hls_url"] = re.sub(r'[^/]+\.douyucdn2\.cn', 'hwa.douyucdn2.cn', d["hls_url"])
                        d["hls_url"] = re.sub(r'fcdn=[^&]+', 'fcdn=hw', d["hls_url"])
                # 过滤 cdnsWithName，只保留华为 CDN
                if "cdnsWithName" in d:
                    d["cdnsWithName"] = [cdn for cdn in d["cdnsWithName"] if cdn.get("cdn") and "hw" in cdn["cdn"].lower()]
                    if not d["cdnsWithName"]:
                        # 如果没有华为 CDN，手动添加一个占位，避免前端无流
                        d["cdnsWithName"] = [{"cdn": "hw-h5", "name": "华为"}]
            # 返回修改后的 JSON
            modified_content = json.dumps(data).encode("utf-8")
            out_headers = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}
            return StreamingResponse(iter([modified_content]), status_code=200, headers=out_headers)
        except Exception as e:
            # 解析失败则原样返回
            print(f"修正斗鱼流地址失败: {e}")

    # 非斗鱼或修正失败，原样返回
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


# ==================== 斗鱼 (返回 crptext 给前端签名，但后端 proxy 会修正流地址) ====================
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

    # 仍然返回 client:true，让前端签名，但后端 proxy 会确保返回的流地址是华为 CDN
    return {
        "client": True,
        "crptext": crptext,
        "roomId": real_id,
        "anchorName": room.get("nickname") or room.get("owner_name") or "斗鱼主播",
        "avatar": room.get("room_icon") or room.get("avatar") or "",
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
