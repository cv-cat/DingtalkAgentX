"""数据模型 — 登录凭证 AuthCredentials."""
from __future__ import annotations

import time
import msgpack


class AuthCredentials:
    """凭证包 (msgpack map 字段 id -> 含义)."""
    FIELDS = {
        1: "domain",            # 'dingding'
        2: "app_key",
        3: "uid",               # int
        4: "nick",
        5: "pinyin",
        7: "secret",            # 24 hex
        8: "session_tag",       # 'B:<32hex>'
        9: "profile",           # 嵌套用户资料
        10: "access_token",     # 'oauth_k1:<b64>'
        11: "access_token2",    # refresh, 'oauth_k1:<b64>'
        12: "expired_time",     # epoch ms (15天)
        13: "token_id",         # '<uid>_<UUID>'
    }

    def __init__(self, raw: dict):
        self.raw = raw
        self.domain = _s(raw.get(1))
        self.app_key = _s(raw.get(2))
        self.uid = raw.get(3)
        self.nick = _s(raw.get(4))
        self.access_token = _s(raw.get(10))
        self.access_token2 = _s(raw.get(11))
        self.expired_time = raw.get(12) or 0
        self.token_id = _s(raw.get(13))
        self.secret = _s(raw.get(7))

    @classmethod
    def from_msgpack(cls, data: bytes) -> "AuthCredentials":
        return cls(msgpack.unpackb(data, raw=False, strict_map_key=False))

    @property
    def expired(self) -> bool:
        return time.time() * 1000 > self.expired_time

    def dump(self) -> dict:
        return {v: getattr(self, v, None) for v in
                ("domain", "app_key", "uid", "nick", "access_token",
                 "access_token2", "expired_time", "token_id")}
