import hashlib
import random
import string
import threading
import time
import ssl
from urllib.parse import urlparse, parse_qs
import websocket

# 从主文件导入共享变量和函数（确保 main.py 中已定义这些）
from main import (
    UA, MOBILE_UA, PROXY_URLS, SOCKS_SUPPORT,
    get_douyin_signature
)

if SOCKS_SUPPORT:
    from python_socks.sync import Proxy

# 导入 Protobuf 定义
from protobuf import douyin


class DouyinBarrageCollector:
    def __init__(self, room_id: str, ttwid: str = "", callback=None):
        self.room_id = room_id
        self.ttwid = ttwid or self._generate_ttwid()
        self.callback = callback
        self.stop_event = threading.Event()
        self.user_unique_id = str(random.randint(1000000000000000000, 9999999999999999999))

    def _generate_ttwid(self) -> str:
        """生成符合抖音规范的 ttwid：19位数字 + 11位随机字母数字"""
        return "".join(random.choices("0123456789", k=19)) + "".join(
            random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=11)
        )

    def _build_ws_url(self, host: str) -> str:
        base_ws_url = (
            f"wss://{host}/webcast/im/push/v2/"
            f"?app_name=douyin_web&version_code=180800&webcast_sdk_version=1.0.14"
            f"&update_version_code=1.0.14&compress=gzip&internal_ext=internal_src:dim"
            f"|wss_push_room_id:{self.room_id}|wss_push_did:0|first_req_ms:{int(time.time() * 1000)}"
            f"|fetch_time:{int(time.time() * 1000)}|seq:1|wss_info:0-0-0-0"
            f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&debug=false"
            f"&endpoint=live&support_wrds=1&im_path=/webcast/im/fetch/&user_unique_id={self.user_unique_id}"
            f"&device_platform=web&cookie_enabled=true&screen_width=1920&screen_height=1080"
            f"&browser_language=zh-CN&browser_platform=Win32&browser_name=Chrome"
            f"&browser_version=120.0.0.0&browser_online=true&tz_name=Asia/Shanghai"
            f"&identity=audience&room_id={self.room_id}&heartbeatDuration=0"
        )

        params_order = (
            "live_id", "aid", "version_code", "webcast_sdk_version",
            "room_id", "sub_room_id", "sub_channel_id", "did_rule",
            "user_unique_id", "device_platform", "device_type", "ac", "identity"
        )
        parsed = urlparse(base_ws_url)
        qs_dict = parse_qs(parsed.query)
        wss_maps = {k: v[0] if isinstance(v, list) else v for k, v in qs_dict.items()}
        param_str = ','.join(f"{p}={wss_maps.get(p, '')}" for p in params_order)
        md5_str = hashlib.md5(param_str.encode()).hexdigest()
        signature = get_douyin_signature(md5_str)
        return f"{base_ws_url}&signature={signature}"

    def _build_headers(self, ua: str, need_ttwid: bool = True):
        headers = {
            "User-Agent": ua,
            "Origin": "https://live.douyin.com",
        }
        if need_ttwid and self.ttwid:
            headers["Cookie"] = f"ttwid={self.ttwid}"
        return headers

    def _on_open(self, ws):
        print(f"[抖音弹幕] 已连接房间 {self.room_id} via {ws._host}")
        def heartbeat():
            while not self.stop_event.is_set():
                time.sleep(10)
                try:
                    if ws.sock and ws.sock.connected:
                        ws.send(b"", opcode=websocket.ABNF.OPCODE_PING)
                except:
                    break
        threading.Thread(target=heartbeat, daemon=True).start()

    def _on_message(self, ws, message):
        if self.stop_event.is_set():
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
                        if chat and chat.user and chat.content and self.callback:
                            self.callback({
                                "type": "chat",
                                "nick": chat.user.nick_name or "匿名用户",
                                "content": chat.content,
                                "time": int(time.time() * 1000)
                            })
                    except Exception as e:
                        print(f"[抖音弹幕] 解析聊天出错: {e}")
                elif method == "WebcastGiftMessage":
                    try:
                        gift = douyin.GiftMessage().parse(payload)
                        if gift and gift.user and self.callback:
                            self.callback({
                                "type": "gift",
                                "nick": gift.user.nick_name or "匿名用户",
                                "gift": gift.gift.name if gift.gift else "礼物",
                                "count": gift.repeat_count,
                                "time": int(time.time() * 1000)
                            })
                    except Exception as e:
                        print(f"[抖音弹幕] 解析礼物出错: {e}")
                elif method == "WebcastLikeMessage":
                    try:
                        like = douyin.LikeMessage().parse(payload)
                        if like and like.user and self.callback:
                            self.callback({
                                "type": "like",
                                "nick": like.user.nick_name or "匿名用户",
                                "count": like.count,
                                "time": int(time.time() * 1000)
                            })
                    except Exception as e:
                        print(f"[抖音弹幕] 解析点赞出错: {e}")
        except Exception as e:
            print(f"[抖音弹幕] 解析顶层错误: {e}")

    def _on_error(self, ws, error):
        print(f"[抖音弹幕] WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        print(f"[抖音弹幕] 连接关闭: {code} {msg}")
        if not self.stop_event.is_set():
            time.sleep(3)
            self.start()

    def start(self):
        configs = [
            {"host": "webcast3-ws-web-lq.snssdk.com", "ua": MOBILE_UA, "need_ttwid": False},
            {"host": "webcast3-ws-web-lq.douyin.com", "ua": UA, "need_ttwid": True},
        ]

        for cfg in configs:
            host = cfg["host"]
            ua = cfg["ua"]
            need_ttwid = cfg["need_ttwid"]

            ws_url = self._build_ws_url(host)
            headers = self._build_headers(ua, need_ttwid)

            print(f"[抖音弹幕] 尝试 {host} (移动端={not need_ttwid})")

            for idx, proxy_url in enumerate(PROXY_URLS):
                try:
                    print(f"[抖音弹幕] 尝试代理 [{idx+1}/{len(PROXY_URLS)}]: {proxy_url}")
                    if SOCKS_SUPPORT and proxy_url and proxy_url.startswith("socks5://"):
                        proxy = Proxy.from_url(proxy_url)
                        sock = proxy.connect(host, 443)
                        ssl_context = ssl.create_default_context()
                        ssl_sock = ssl_context.wrap_socket(sock, server_hostname=host)
                        ws = websocket.WebSocketApp(
                            ws_url, header=headers,
                            on_open=self._on_open, on_message=self._on_message,
                            on_error=self._on_error, on_close=self._on_close,
                            socket=ssl_sock
                        )
                        ws._host = host
                    elif proxy_url and (proxy_url.startswith("http://") or proxy_url.startswith("https://")):
                        proxy_parts = urlparse(proxy_url)
                        ws = websocket.WebSocketApp(
                            ws_url, header=headers,
                            on_open=self._on_open, on_message=self._on_message,
                            on_error=self._on_error, on_close=self._on_close,
                            http_proxy_host=proxy_parts.hostname,
                            http_proxy_port=proxy_parts.port,
                            http_proxy_auth=(proxy_parts.username, proxy_parts.password) if proxy_parts.username else None,
                            proxy_type="http"
                        )
                        ws._host = host
                    else:
                        ws = websocket.WebSocketApp(
                            ws_url, header=headers,
                            on_open=self._on_open, on_message=self._on_message,
                            on_error=self._on_error, on_close=self._on_close,
                        )
                        ws._host = host
                    ws.run_forever()
                    return
                except Exception as e:
                    print(f"[抖音弹幕] 代理 {proxy_url} 失败: {e}")
                    if idx == len(PROXY_URLS) - 1:
                        break
                    time.sleep(1)
            print(f"[抖音弹幕] {host} 所有代理失败，尝试下一个接入点...")

        print("[抖音弹幕] 所有接入点和代理均失败，停止重试")
