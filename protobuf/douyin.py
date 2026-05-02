# protobuf/douyin.py (移除 msg_type，彻底避免 uint64 错误)
from dataclasses import dataclass
from typing import List, Optional
import betterproto


@dataclass
class PushFrame(betterproto.Message):
    seq_id: int = betterproto.uint64_field(1)
    log_id: int = betterproto.uint64_field(2)
    service: int = betterproto.uint64_field(3)
    method: int = betterproto.uint64_field(4)
    headers_list: List["HeadersList"] = betterproto.message_field(5)
    payload_encoding: str = betterproto.string_field(6)
    payload_type: str = betterproto.string_field(7)
    payload: bytes = betterproto.bytes_field(8)


@dataclass
class HeadersList(betterproto.Message):
    key: str = betterproto.string_field(1)
    value: str = betterproto.string_field(2)


@dataclass
class Response(betterproto.Message):
    messages_list: List["Message"] = betterproto.message_field(1)
    cursor: str = betterproto.string_field(2)
    fetch_interval: int = betterproto.uint64_field(3)
    now: int = betterproto.uint64_field(4)
    internal_ext: str = betterproto.string_field(5)


@dataclass
class Message(betterproto.Message):
    method: str = betterproto.string_field(1)
    payload: bytes = betterproto.bytes_field(2)
    msg_id: int = betterproto.int64_field(3)
    # msg_type 字段已移除，防止解析错误
    offset: int = betterproto.int64_field(5)


@dataclass
class ChatMessage(betterproto.Message):
    common: "Common" = betterproto.message_field(1)
    user: "User" = betterproto.message_field(2)
    content: str = betterproto.string_field(3)
    visible_to_sender: bool = betterproto.bool_field(4)
    text_effect: "TextEffect" = betterproto.message_field(5)
    background_image: "Image" = betterproto.message_field(6)


@dataclass
class GiftMessage(betterproto.Message):
    common: "Common" = betterproto.message_field(1)
    gift_id: int = betterproto.uint64_field(2)
    group_id: int = betterproto.uint64_field(3)
    repeat_count: int = betterproto.uint64_field(4)
    combo: int = betterproto.uint64_field(5)
    user: "User" = betterproto.message_field(6)
    to_user: "User" = betterproto.message_field(7)
    gift: "Gift" = betterproto.message_field(8)
    describe: str = betterproto.string_field(9)
    combo_total: int = betterproto.uint64_field(10)


@dataclass
class LikeMessage(betterproto.Message):
    common: "Common" = betterproto.message_field(1)
    count: int = betterproto.uint64_field(2)
    total: int = betterproto.uint64_field(3)
    color: int = betterproto.uint64_field(4)
    user: "User" = betterproto.message_field(5)
    icon: str = betterproto.string_field(6)


@dataclass
class Common(betterproto.Message):
    method: str = betterproto.string_field(1)
    msg_id: int = betterproto.uint64_field(2)
    room_id: int = betterproto.uint64_field(3)
    create_time: int = betterproto.uint64_field(4)
    monitor: int = betterproto.uint64_field(5)
    show_msg: bool = betterproto.bool_field(6)
    fold_type: bool = betterproto.bool_field(7)


@dataclass
class User(betterproto.Message):
    id: int = betterproto.uint64_field(1)
    short_id: int = betterproto.uint64_field(2)
    nick_name: str = betterproto.string_field(3)
    gender: int = betterproto.uint64_field(4)
    signature: str = betterproto.string_field(5)
    level: int = betterproto.uint32_field(6)
    birthday: int = betterproto.uint64_field(7)
    telephone: str = betterproto.string_field(8)
    avatar_thumb: "Image" = betterproto.message_field(9)
    avatar_medium: "Image" = betterproto.message_field(10)
    avatar_large: "Image" = betterproto.message_field(11)
    verified: bool = betterproto.bool_field(12)
    experience: int = betterproto.uint32_field(13)
    city: str = betterproto.string_field(14)
    status: int = betterproto.int32_field(15)
    create_time: int = betterproto.uint64_field(16)
    modify_time: int = betterproto.uint64_field(17)
    secret: int = betterproto.uint32_field(18)
    share_qrcode_uri: str = betterproto.string_field(19)
    income_share_percent: int = betterproto.uint32_field(20)
    badge_image_list: List["Image"] = betterproto.message_field(21)
    border: str = betterproto.string_field(22)
    medal_id: str = betterproto.string_field(23)


@dataclass
class Image(betterproto.Message):
    url_list: List[str] = betterproto.string_field(1)
    uri: str = betterproto.string_field(2)
    height: int = betterproto.uint64_field(3)
    width: int = betterproto.uint64_field(4)
    avg_color: str = betterproto.string_field(5)
    image_type: int = betterproto.uint32_field(6)
    open_web_url: str = betterproto.string_field(7)
    content: "Content" = betterproto.message_field(8)
    is_animated: bool = betterproto.bool_field(9)


@dataclass
class Content(betterproto.Message):
    name: str = betterproto.string_field(1)
    font_color: str = betterproto.string_field(2)
    level: int = betterproto.uint64_field(3)
    alternative_text: str = betterproto.string_field(4)


@dataclass
class TextEffect(betterproto.Message):
    portrait: "TextEffectDetail" = betterproto.message_field(1)
    landscape: "TextEffectDetail" = betterproto.message_field(2)


@dataclass
class TextEffectDetail(betterproto.Message):
    background: "TextFormat" = betterproto.message_field(1)
    text: "TextFormat" = betterproto.message_field(2)


@dataclass
class TextFormat(betterproto.Message):
    color: str = betterproto.string_field(1)
    bold: bool = betterproto.bool_field(2)
    italic: bool = betterproto.bool_field(3)
    weight: int = betterproto.uint32_field(4)
    italic_angle: int = betterproto.uint32_field(5)
    font_size: int = betterproto.uint32_field(6)


@dataclass
class Gift(betterproto.Message):
    id: int = betterproto.uint64_field(1)
    name: str = betterproto.string_field(2)
    describe: str = betterproto.string_field(3)
    type: int = betterproto.uint32_field(4)
    diamond_count: int = betterproto.uint32_field(5)
    duration: int = betterproto.uint32_field(6)
    icon: "Image" = betterproto.message_field(7)
    gift_image: "Image" = betterproto.message_field(8)