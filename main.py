import asyncio
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from streamget import (
    HuyaLiveStream, DouyuLiveStream, BilibiliLiveStream, DouyinLiveStream
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

async def parse(url: str):
    if "huya.com" in url:
        live = HuyaLiveStream()
    elif "douyu.com" in url:
        live = DouyuLiveStream()
    elif "bilibili.com" in url:
        live = BilibiliLiveStream()
    elif "douyin.com" in url:
        live = DouyinLiveStream()
    else:
        raise HTTPException(400, "不支持的平台")
    
    data = await live.fetch_web_stream_data(url)
    stream = await live.fetch_stream_url(data, "OD")  # OD = 原画
    return stream.to_dict()

@app.get("/api/parse")
async def api_parse(url: str = Query(...)):
    try:
        return await parse(url)
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/")
def root():
    return {"status": "ok", "message": "多平台直播解析 API"}