import json
import re
import httpx
import asyncio
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from streamget import HuyaLiveStream, DouyuLiveStream, BilibiliLiveStream, DouyinLiveStream

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ==================== 工具 ====================

def build_streams(flv: str, m3u8: str) -> list:
    streams = []
    if flv and flv.startswith("http"):
        streams.append({"cdn": "FLV", "url": flv, "type": "flv"})
    if m3u8 and m3u8.startswith("http"):
        streams.append({"cdn": "HLS", "url": m3u8, "type": "m3u8"})
    return streams


# ==================== 虎牙（streamget + 弹幕参数） ====================

async def fetch_huya_danmaku_params(room_id: str) -> dict:
    """抓虎牙移动端页面，提取弹幕所需参数"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://m.huya.com/{room_id}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/100 Mobile Safari/537.36",
                    "Referer": "https://www.huya.com/",
                }
            )
            html = r.text
            ayyuid  = int((re.search(r'"lYyid":(\d+)', html) or re.search(r'ayyuid:\s*["\']?(\d+)', html) or [None, 0])[1])
            top_sid = int((re.search(r'"lChannelId":(\d+)', html) or [None, 0])[1])
            sub_sid = int((re.search(r'"lSubChannelId":(\d+)', html) or [None, 0])[1])
            return {"platform": "huya", "ayyuid": ayyuid, "topSid": top_sid, "subSid": sub_sid}
    except Exception:
        return {}


async def parse_huya(url: str) -> dict:
    room_id = url.rstrip("/").split("/")[-1].split("?")[0]
    live = HuyaLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"虎牙未获取到流地址: {raw}")
    danmaku = await fetch_huya_danmaku_params(room_id)
    return {"streams": streams, "title": raw.get("anchor_name", "虎牙主播"), "avatar": raw.get("avatar", ""), "danmaku": danmaku}


# ==================== 斗鱼（streamget，弹幕前端直连） ====================

async def parse_douyu(url: str) -> dict:
    live = DouyuLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"斗鱼未获取到流地址: {raw}")
    return {"streams": streams, "title": raw.get("anchor_name", "斗鱼主播"), "avatar": raw.get("avatar", "")}


# ==================== B站（直接调官方 API，避免 streamget 在美国IP失败） ====================

async def parse_bilibili(url: str) -> dict:
    rid = url.rstrip("/").split("/")[-1].split("?")[0]
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    headers = {"User-Agent": ua, "Referer": "https://live.bilibili.com/"}

    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        # 获取真实房间号
        room_resp = await client.get(f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}")
        room_data = room_resp.json()
        if room_data.get("code") != 0:
            raise ValueError(f"B站房间信息获取失败: {room_data.get('message')}")
        real_rid = room_data["data"]["room_id"]
        if room_data["data"].get("live_status") != 1:
            raise ValueError("B站：未开播")

        # 获取流地址（不需要登录的低画质）
        play_resp = await client.get(
            f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
            f"?room_id={real_rid}&protocol=0,1&format=0,1,2&codec=0,1&qn=10000&platform=web&ptype=8"
        )
        play_data = play_resp.json()
        if play_data.get("code") != 0:
            raise ValueError(f"B站播流失败: {play_data.get('message')}")

        playurl = play_data["data"].get("playurl_info", {}).get("playurl", {})
        streams = []
        seen = set()
        for stream in playurl.get("stream", []):
            for fmt in stream.get("format", []):
                for codec in fmt.get("codec", []):
                    for info in codec.get("url_info", []):
                        full_url = info["host"] + codec["base_url"] + info["extra"]
                        if full_url not in seen:
                            seen.add(full_url)
                            typ = "flv" if fmt["format_name"] == "flv" else "m3u8"
                            cdn_m = re.search(r"([a-z0-9]+)\.bilivideo", info["host"])
                            streams.append({"cdn": f"{fmt['format_name'].upper()}-{cdn_m.group(1) if cdn_m else 'cdn'}", "url": full_url, "type": typ})

        streams.sort(key=lambda x: 0 if x["type"] == "flv" else 1)
        if not streams:
            raise ValueError("B站：流地址提取失败")

        # 主播信息
        anchor_name, avatar = "B站主播", ""
        try:
            info_resp = await client.get(f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={real_rid}")
            info = info_resp.json().get("data", {}).get("room_info", {})
            anchor_name = info.get("uname") or info.get("owner_name") or anchor_name
            avatar = info.get("face") or avatar
        except Exception:
            pass

        return {"streams": streams[:4], "title": anchor_name, "avatar": avatar}


# ==================== 抖音（streamget） ====================

async def parse_douyin(url: str) -> dict:
    live = DouyinLiveStream()
    data = await live.fetch_web_stream_data(url, process_data=True)
    stream_obj = await live.fetch_stream_url(data, "OD")
    raw = json.loads(stream_obj.to_json())
    streams = build_streams(raw.get("flv_url", ""), raw.get("m3u8_url", ""))
    if not streams:
        raise ValueError(f"抖音未获取到流地址: {raw}")
    return {"streams": streams, "title": raw.get("anchor_name", "抖音主播"), "avatar": raw.get("avatar", "")}


# ==================== 路由 ====================

@app.get("/api/parse")
async def api_parse(url: str = Query(...)):
    try:
        if "huya.com" in url:
            return await parse_huya(url)
        elif "douyu.com" in url:
            return await parse_douyu(url)
        elif "bilibili.com" in url:
            return await parse_bilibili(url)
        elif "douyin.com" in url:
            return await parse_douyin(url)
        else:
            raise HTTPException(status_code=400, detail="不支持的平台，仅支持虎牙/斗鱼/B站/抖音")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug")
async def api_debug(url: str = Query(...)):
    try:
        if "huya.com" in url:
            live = HuyaLiveStream()
        elif "douyu.com" in url:
            live = DouyuLiveStream()
        elif "bilibili.com" in url:
            live = BilibiliLiveStream()
        elif "douyin.com" in url:
            live = DouyinLiveStream()
        else:
            return {"error": "不支持的平台"}
        data = await live.fetch_web_stream_data(url, process_data=True)
        return {"raw_data": data}
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}


@app.get("/")
def root():
    return {"status": "ok", "message": "多平台直播解析 API"}
