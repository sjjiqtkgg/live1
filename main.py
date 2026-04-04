import json
import asyncio
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from streamget import (
    HuyaLiveStream,
    DouyuLiveStream,
    BilibiliLiveStream,
    DouyinLiveStream,
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def streamdata_to_response(stream_obj) -> dict:
    """
    把 streamget 的 StreamData 转成前端期待的格式：
    { streams: [{cdn, url, type}], title, avatar }
    """
    raw = json.loads(stream_obj.to_json())

    streams = []
    flv = raw.get("flv_url") or ""
    m3u8 = raw.get("m3u8_url") or ""

    if flv and flv.startswith("http"):
        streams.append({"cdn": "FLV", "url": flv, "type": "flv"})
    if m3u8 and m3u8.startswith("http"):
        streams.append({"cdn": "HLS", "url": m3u8, "type": "m3u8"})

    if not streams:
        raise ValueError("streamget 未返回有效流地址（直播可能未开播）")

    return {
        "streams": streams,
        "title": raw.get("anchor_name") or "主播",
        "avatar": raw.get("avatar") or "",
    }


async def parse_url(url: str) -> dict:
    if "huya.com" in url:
        live = HuyaLiveStream()
    elif "douyu.com" in url:
        live = DouyuLiveStream()
    elif "bilibili.com" in url:
        live = BilibiliLiveStream()
    elif "douyin.com" in url:
        live = DouyinLiveStream()
    else:
        raise HTTPException(status_code=400, detail="不支持的平台，仅支持虎牙/斗鱼/B站/抖音")

    # process_data=True 是 fetch_stream_url 的前提
    data = await live.fetch_web_stream_data(url, process_data=True)

    if not data.get("is_live"):
        raise HTTPException(status_code=200, detail=f"{data.get('anchor_name','主播')} 未开播")

    stream_obj = await live.fetch_stream_url(data, "OD")
    return streamdata_to_response(stream_obj)


@app.get("/api/parse")
async def api_parse(url: str = Query(..., description="直播间地址")):
    try:
        result = await parse_url(url)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
def root():
    return {"status": "ok", "message": "多平台直播解析 API"}
