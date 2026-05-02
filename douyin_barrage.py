import gzip
import hashlib
import random
import string
import threading
import time
import ssl
from urllib.parse import urlparse, parse_qs
import websocket

from main import (
    UA, MOBILE_UA, PROXY_URLS, SOCKS_SUPPORT,
    get_douyin_signature
)

if SOCKS_SUPPORT:
    from python_socks.sync import Proxy

from protobuf import douyin


class DouyinBarrageCollector:
    # 全套 WebSocket 域名，按优先级排序，失败自动切换
    WS_DOMAINS = [
        "webcast3-ws-web-lq.douyin.com",
        "webcast5-ws-web-lf.douyin.com",
        "webcast3-ws-web-hl.douyin.com",
        "webcast3-ws-web-bj.douyin.com"
    ]

    def __init__(self, room_id: str, ttwid: str = "", callback=None):
        self.room_id = room_id
        self.ttwid = ttwid or self._generate_ttwid()
        self.callback = callback
        self.stop_event = threading.Event()
        self.user_unique_id = str(random.randint(1000000000000000000, 9999999999999999999))

        # 指数退避重连参数
        self.retry_count = 0
        self.max_retry_backoff = 60  # 最大重连间隔

    def _generate_ttwid(self) -> str:
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

    def _build_headers(self, ua: str):
        headers = {
            "User-Agent": ua,
            "Origin": "https://live.douyin.com",
            "Cookie": f"ttwid={self.ttwid}"
        }
        return headers

    def _on_open(self, ws):
        print(f"[抖音弹幕] 已连接房间 {self.room_id} via {ws._host}")
        self.retry_count = 0  # 连接成功，重置重试计数

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
            payload = push_frame.payload
            if push_frame.payload_encoding == "gzip":
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            response = douyin.Response().parse(payload)

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
            print(f"[抖音弹幕] 解析 PushFrame/Response 错误: {e}")

    def _on_error(self, ws, error):
        print(f"[抖音弹幕] WebSocket 错误: {error}")

    def _on_close(self, ws, code, msg):
        print(f"[抖音弹幕] 连接关闭: {code} {msg}")
        if self.stop_event.is_set():
            print("[抖音弹幕] 已收到停止信号，不再重连")
            return
        # 指数退避重连
        self.retry_count += 1
        wait_time = min(1.6 ** self.retry_count, self.max_retry_backoff)
        print(f"[抖音弹幕] {wait_time:.1f} 秒后重连...")
        time.sleep(wait_time)
        self.start()

    def start(self):
        headers = self._build_headers(UA)

        # 遍历多个服务器域名
        for host in self.WS_DOMAINS:
            ws_url = self._build_ws_url(host)
            print(f"[抖音弹幕] 尝试 {host}")

            # 遍历代理列表
            proxies = PROXY_URLS if PROXY_URLS else [None]
            for idx, proxy_url in enumerate(proxies):
                if self.stop_event.is_set():
                    return
                try:
                    print(f"[抖音弹幕] 尝试代理 [{idx+1}/{len(proxies)}]: {proxy_url or '直连'}")
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
                    elif proxy_url and proxy_url.startswith(("http://", "https://")):
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
                    # run_forever 会阻塞直到连接关闭
                    ws.run_forever()
                    return
                except Exception as e:
                    print(f"[抖音弹幕] 连接失败 {host}: {e}")
                    time.sleep(1)
                    continue

            print(f"[抖音弹幕] 域名 {host} 所有代理失败，尝试下一个域名")

        print("[抖音弹幕] 所有域名和代理均失败，停止重试")