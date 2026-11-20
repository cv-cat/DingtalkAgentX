"""消息收发 + 搜索. 请求头序/头数量/len/body 字段需与客户端格式一致."""
from __future__ import annotations

import json
import random
import string
import time
import uuid as uuidlib
import zlib
from typing import Callable

from . import consts
from .lwp.client import LwpClient
from .lwp.wire import RPC_URIS, pack

MessageHandler = Callable[[dict], None]

_SEND_ALPHABET = string.ascii_letters + string.digits + "@#$"


def send_uuid() -> str:
    return "".join(random.choices(_SEND_ALPHABET, k=8))


def send_text_body(text: str, conversation_id: str, org_id: str, nick: str,
                   avatar: str, uuid8: str | None = None) -> bytes:
    """IDLSend/send 9 个顶层键, 顺序 1,2,3,4,5,6,7,8,18. 缺 18 或 oid 服务端会拒."""
    ext = json.dumps({"empVer": 0, "orgId": str(org_id)}, separators=(",", ":"))
    return pack({
        1: uuid8 or send_uuid(),
        2: str(conversation_id),
        3: 1,
        4: 1,
        5: {1: 1, 2: {1: text}, 7: []},
        6: 1,
        7: {"oid": str(org_id)},
        8: nick,
        18: {1: 5, 2: ext, 4: avatar},
    })


def send_ext_body(text: str, conversation_id: str, org_id: str, nick: str,
                  avatar: str, markdown: str | None = None, title: str = "",
                  reply_mid: int | None = None, mentions: dict | None = None,
                  uuid8: str | None = None) -> bytes:
    """IDLSend/send 扩展内容体 (markdown / 引用回复 / @提及, 可并存).

    - f5.7 = [ext 元素] 数组, 每个元素 {1: 1204, 2: "", 3: 0, 5: false, 6: {…}}:
      * markdown:     6={"markdown": md, "title": t}
      * reply:        6={"replyMessageId": str(mid)}
    - f5.8 = {uid_str: 昵称} @提及表 — 正文里写 @{昵称}; 服务端落库 f7.8
      并生成 f7.12=[{1:uid,2:1}] 被@标记列表
    f5.1 内容类型表: 1=text 2/5/6=photo(需图片模型) 3/7=audio 4=file —
    其他值报 "...Content is null" 业务错. 1204 是唯一合法 ext 类型码,
    1205/1206 触发 139001 decode 拒."""
    m = {}
    if markdown is not None:
        m["markdown"] = markdown
        if title:
            m["title"] = title
    if reply_mid is not None:
        m["replyMessageId"] = str(int(reply_mid))
    ext = json.dumps({"empVer": 0, "orgId": str(org_id)}, separators=(",", ":"))
    f5 = {1: 1, 2: {1: text}, 7: []}
    if m:
        f5[7] = [{1: 1204, 6: m}]
    if mentions:
        f5[8] = {str(int(u)): str(n) for u, n in mentions.items()}
    return pack({
        1: uuid8 or send_uuid(),
        2: str(conversation_id),
        3: 1,
        4: 1,
        5: f5,
        6: 1,
        7: {"oid": str(org_id)},
        8: nick,
        18: {1: 5, 2: ext, 4: avatar},
    })


def get_state_body(ts_ms: int, channel: str = "syncc", unit: str = "center,1",
                   pipeline: str = "feynman") -> bytes:
    """9 键. 含 7. ackDiff 没有 7, 不能混用."""
    return pack({
        1: -1, 2: 0, 3: int(ts_ms), 4: 0,
        5: unit, 6: pipeline, 7: 0,
        11: channel, 12: channel,
    })


def ack_diff_body(ts_ms: int, channel: str = "syncc", unit: str = "center,1",
                  pipeline: str = "feynman") -> bytes:
    """8 键, 无字段 7. /s/feynman 回执: center,1 / feynman / syncc."""
    return pack({
        1: -1, 2: 0, 3: int(ts_ms), 4: 0,
        5: unit, 6: pipeline,
        11: channel, 12: channel,
    })


def ack_for_push(topic: str, parsed: dict, ts_ms: int) -> bytes:
    """按推送尾部的频道名组 ack."""
    tail = {}
    if isinstance(parsed, dict):
        raw = parsed.get("3") or parsed.get(3) or {}
        if isinstance(raw, dict):
            tail = raw
    channel = str(tail.get("9") or tail.get(9) or tail.get("10") or tail.get(10) or "syncc")
    pipeline = topic.rsplit("/", 1)[-1] or "feynman"
    if channel == "syncc":
        pipeline = "feynman"
    return ack_diff_body(ts_ms, channel=channel, pipeline=pipeline)


def update_to_view_body(msg_id: int, conversation_id: str) -> bytes:
    return pack({1: int(msg_id), 2: str(conversation_id), 3: "im", 4: 1, 5: 0, 6: 0})


def get_by_ids_body(conversation_ids: list) -> bytes:
    """IDLConversation/getByIds: 裸数组 [cid, ...]."""
    return pack([str(c).encode() for c in conversation_ids])


def list_tab_body(conversation_id: str) -> bytes:
    """ConvNavTabI/listTab: {1: cid}."""
    return pack({1: str(conversation_id)})


def my_agent_cid_body(org_id) -> bytes:
    """AIAgentI/getOrCreateMyAgentCid: {1: org_id_int}."""
    return pack({1: int(org_id)})


def emotion_list_body(ts_ms: int) -> bytes:
    """TextEmotionI/listCustomTextEmotion: {1: ts_ms}."""
    return pack({1: int(ts_ms)})


def list_newest_ext_body() -> bytes:
    """listNewestExtV3: 空 JSON 对象 {} (wukongidl JSON 编码族). 返回全量会话列表."""
    return b"{}"


def _fixstr(s: str) -> bytes:
    """msgpack fixstr (0xa0|len). LWP 位置参数流要求 fixstr 编码, str8/str16 会被服务端拒."""
    b = s.encode()
    if len(b) < 32:
        return bytes([0xa0 | len(b)]) + b
    return b"\xd9" + bytes([len(b)]) + b


def positional_body(*values) -> bytes:
    """LWP 位置参数流: concatenated msgpack 值 (无数组包装).

    - getByIdUnlimited:  (cid_fixstr) → 会话模型
    - listMembers:       (cid_fixstr, 0, 100) → [{member}]
    - listMessagesPagination: (cid_fixstr, nil, int64_mid, count) → 消息列表
    p1 过解码后 p2 缺 → 'Parameter not found: N'; 类型错 → 'Decode parameter error: N'.
    nil/bool/int/fixstr 按序接受; 顶层 map/array 出现即重置参数计数."""
    out = b""
    for v in values:
        if v is None:
            out += b"\xc0"
        elif v is True:
            out += b"\xc3"
        elif v is False:
            out += b"\xc2"
        elif isinstance(v, int):
            if -32 <= v < 128:
                out += bytes([v & 0xFF])
            elif 0 <= v < 0x10000:
                out += b"\xcd" + v.to_bytes(2, "big")
            else:
                out += b"\xd3" + v.to_bytes(8, "big", signed=True)
        else:
            out += _fixstr(str(v))
    return out


def list_members_body(conversation_id: str, offset: int = 0, count: int = 100) -> bytes:
    """IDLConversation/listMembers: 位置流 (cid, offset, count)."""
    return positional_body(str(conversation_id), offset, count)


def pagination_body(conversation_id: str, msg_id: int | None, count: int = 20) -> bytes:
    """IDLMessage/listMessagesPagination: 位置流 (cid, nil, int64 mid, count).

    mid 必须是会话内真实消息 ID (getByIdUnlimited/feed_chats 的 lastMsg);
    0 → 130050 '云端仅支持权益期内的消息' (免费版历史窗口限制)."""
    return positional_body(str(conversation_id), None, int(msg_id or 0), count)


def get_by_id_unlimited_body(conversation_id: str) -> bytes:
    """IDLConversation/getByIdUnlimited: 裸 fixstr(cid) 单参数 → 44键会话模型."""
    return _fixstr(str(conversation_id))


def get_msg_by_id_with_cid_body(conversation_id: str, msg_id: int) -> bytes:
    """IDLMessage/getMessageByIdWithCidV2: map {1: cid, 2: mid} → 单条完整消息 (316B)."""
    return pack({1: str(conversation_id), 2: int(msg_id)})


def pull_ding_body(next_page: bool = False) -> bytes:
    """IDLDing/pullDingListOrderBySendAt: 裸 bool."""
    return pack(bool(next_page))


def search_tabs_body(org_id: str, protocol_ver: int = 15) -> bytes:
    # 字段序: tabId, searchScopeModel.microOrgId, version, protocolVer. 裸 JSON, 无空格.
    return (
        '[{"tabId":200,"searchScopeModel":{"microOrgId":"%s"},"version":"","protocolVer":%d}]'
        % (org_id, protocol_ver)
    ).encode()


def search_feed_body(org_id: str, request_id: str | None = None) -> bytes:
    rid = request_id or str(uuidlib.uuid4())
    return (
        '[{"requestId":"%s","tabType":200,"orgId":%s,"protocolVer":7}]' % (rid, org_id)
    ).encode()


def search_hot_body(org_id: str, request_id: str | None = None) -> bytes:
    rid = request_id or str(uuidlib.uuid4())
    return (
        '[{"offset":"0","size":-1,"orgId":"%s","requestId":"%s"}]' % (org_id, rid)
    ).encode()


def corpid_body(org_id: str) -> bytes:
    return ("[%s]" % org_id).encode()


def authorize_body(ticket: str) -> bytes:
    return pack({
        2: "Windows 10.0 Native",
        3: "Windows 10.0",
        4: ticket,
    })


def upload_dev_body(mac_colon: str, ticket: str) -> bytes:
    return pack({1: mac_colon, 2: ticket})


def ssid_body(ticket: str, mac_colon: str) -> bytes:
    return pack_pair_strs(ticket, mac_colon)


def pack_pair_strs(a: str, b: str) -> bytes:
    from .lwp.wire import pack_values
    return pack_values(a, b)


class DingTalkApi:
    def __init__(self, client: LwpClient, org_id: str | None = None,
                 nick: str = "", avatar: str = "", uid: int | str | None = None):
        self.client = client
        self.org_id = org_id or consts.ORG_ID
        self.nick = nick
        self.avatar = avatar
        self.uid = int(uid) if uid else 0
        self._handlers: list[MessageHandler] = []

    def on_message(self, handler: MessageHandler):
        self._handlers.append(handler)

    async def call(self, uri: str, body: bytes = b"", profile: str | None = None):
        """任意已登记接口. 头型由 URI + 连接阶段决定, 不接受少头."""
        path = uri[4:].strip() if uri.startswith("LWP") else uri
        if path not in RPC_URIS and (" " + path) not in RPC_URIS and path.lstrip() not in RPC_URIS:
            raise KeyError(f"uri not in RPC catalog: {uri}")
        # 双空格起点的日历接口
        send_uri = uri
        if path == "/r/Adaptor/IDLCalendar/batchGetEventTodos":
            send_uri = " /r/Adaptor/IDLCalendar/batchGetEventTodos"
        return await self.client.request(send_uri, body, profile=profile)

    async def register(self, token: str, body: bytes, profile: str = "reg_small"):
        self.client.token = token
        if not token.startswith("oauth_k1:"):
            raise ValueError("token must be oauth_k1:")
        resp = await self.client.request("/reg", body, profile=profile)
        if resp.status == 200:
            self.client.header_mode = "session"
        return resp

    async def get_state(self, ts_ms: int | None = None):
        body = get_state_body(ts_ms or int(time.time() * 1000))
        return await self.client.request(consts.URI_SYNC_GET_STATE, body, profile="warm")

    async def ack_diff(self, ts_ms: int):
        body = ack_diff_body(ts_ms)
        return await self.client.request(consts.URI_SYNC_ACK_DIFF, body, profile="warm")

    async def send_text(self, conversation_id: str, text: str, nick: str | None = None,
                        avatar: str | None = None, org_id: str | None = None) -> dict:
        body = send_text_body(
            text, conversation_id, org_id or self.org_id,
            nick or self.nick, avatar or self.avatar,
        )
        resp = await self.client.request(consts.URI_SEND_MSG, body, profile="warm")
        return {"status": resp.status, "body": resp.body, "len": len(body)}

    async def send_markdown(self, conversation_id: str, markdown: str, title: str = "",
                            nick: str | None = None, avatar: str | None = None,
                            org_id: str | None = None):
        """发 markdown 消息 (f5.7 ext 1204, 6={"markdown","title"}). 正文 text 同时传摘要."""
        body = send_ext_body(markdown[:64], conversation_id, org_id or self.org_id,
                             nick or self.nick, avatar or self.avatar,
                             markdown=markdown, title=title)
        resp = await self.client.request(consts.URI_SEND_MSG, body, profile="warm")
        return {"status": resp.status, "body": resp.body, "len": len(body)}

    async def send_reply(self, conversation_id: str, text: str, reply_mid: int,
                         markdown: str | None = None, title: str = "",
                         nick: str | None = None, avatar: str | None = None,
                         org_id: str | None = None):
        """引用回复 (f5.7 ext 1204, 6={"replyMessageId"}). 可同时带 markdown."""
        body = send_ext_body(text, conversation_id, org_id or self.org_id,
                             nick or self.nick, avatar or self.avatar,
                             markdown=markdown, title=title, reply_mid=reply_mid)
        resp = await self.client.request(consts.URI_SEND_MSG, body, profile="warm")
        return {"status": resp.status, "body": resp.body, "len": len(body)}

    async def send_mention(self, conversation_id: str, text: str,
                           mentions: dict, markdown: str | None = None,
                           nick: str | None = None, avatar: str | None = None,
                           org_id: str | None = None):
        """@提及消息 (f5.8 = {uid: 昵称}).

        正文 text 里写 @{昵称} 占位; mentions = {uid: 昵称}.
        服务端落库 f7.8={uid:nick} + f7.12=[{1:uid,2:1}] 被@标记."""
        body = send_ext_body(text, conversation_id, org_id or self.org_id,
                             nick or self.nick, avatar or self.avatar,
                             markdown=markdown, mentions=mentions)
        resp = await self.client.request(consts.URI_SEND_MSG, body, profile="warm")
        return {"status": resp.status, "body": resp.body, "len": len(body)}

    async def mark_viewed(self, msg_id: int, conversation_id: str):
        body = update_to_view_body(msg_id, conversation_id)
        return await self.client.request("/r/IDLMessageStatus/updateToViewV2", body, profile="warm")

    # ---- 资料 / 会话 ----

    async def get_user_profile(self, uid: int):
        """UserMixI/getUserProfileByUid: 裸 uid → {1:uid,2:nick,3:pinyin,33:org_json,46:phone_mask}."""
        body = pack(int(uid))
        return await self.client.request("/r/Adaptor/UserMixI/getUserProfileByUid", body, profile="warm")

    async def get_user_profiles(self, uids: list):
        """UserMixI/getUserProfileByUids: 裸 [uid, ...]."""
        body = pack([int(u) for u in uids])
        return await self.client.request("/r/Adaptor/UserMixI/getUserProfileByUids", body, profile="warm")

    async def get_my_profile(self):
        """UserMixI/getMyUserProfileExtension: 空 body → 全量自己资料 (26743B)."""
        return await self.client.request("/r/Adaptor/UserMixI/getMyUserProfileExtension", b"", profile="warm")

    async def get_user_profile_ext(self, uid: int):
        """UserMixI/getUserProfileExtensionByUid: 裸 uid → 4968B 扩展资料."""
        return await self.client.request("/r/Adaptor/UserMixI/getUserProfileExtensionByUid", pack(int(uid)), profile="warm")

    async def get_chat_info(self, conversation_ids: list, compensated: bool = True):
        """会话详情 (get_chat_info/get_group_name). 裸数组 [cid]; compensated 版响应多 f45 成员 org JSON."""
        body = get_by_ids_body(conversation_ids)
        uri = "/r/IDLConversation/listByIdsForCompensated" if compensated else "/r/IDLConversation/getByIds"
        return await self.client.request(uri, body, profile="warm")

    async def list_nav_tabs(self, conversation_id: str):
        body = list_tab_body(conversation_id)
        return await self.client.request("/r/Adaptor/ConvNavTabI/listTab", body, profile="warm")

    async def my_agent_cid(self, org_id=None):
        """AIAgentI/getOrCreateMyAgentCid → 我的 AI 助手会话列表 (1220B)."""
        body = my_agent_cid_body(org_id if org_id is not None else self.org_id)
        return await self.client.request("/r/Adaptor/AIAgentI/getOrCreateMyAgentCid", body, profile="warm")

    async def list_emotions(self, ts_ms: int | None = None):
        body = emotion_list_body(ts_ms or int(time.time() * 1000))
        return await self.client.request("/r/Adaptor/TextEmotionI/listCustomTextEmotion", body, profile="warm")

    async def pull_ding_list(self, next_page: bool = False):
        body = pull_ding_body(next_page)
        return await self.client.request("/r/Adaptor/IDLDing/pullDingListOrderBySendAt", body, profile="warm")

    async def feed_chats(self):
        """listNewestExtV3 → 全量会话列表."""
        body = list_newest_ext_body()
        return await self.client.request("/r/IDLConversation/listNewestExtV3", body, profile="warm")

    # ---- members / pull_history (位置参数流族) ----

    async def get_chat_info_unlimited(self, conversation_id: str):
        """getByIdUnlimited: 裸 fixstr(cid) → 44键会话模型 (含 lastMsg 完整信封).

        比 getByIds 多 f1.44-51 区段; f2[0].1 = 最新消息 (pagination 的锚点 mid)."""
        body = get_by_id_unlimited_body(conversation_id)
        return await self.client.request("/r/IDLConversation/getByIdUnlimited", body, profile="warm")

    async def list_members(self, conversation_id: str, offset: int = 0, count: int = 100):
        """IDLConversation/listMembers: (cid, offset, count) → [{1:{1:uid},2:role,3:{1:nick},5:joinTs}].

        role: 1=群主 3=成员; 大群 100/页上限."""
        body = list_members_body(conversation_id, offset, count)
        return await self.client.request("/r/IDLConversation/listMembers", body, profile="warm")

    async def pull_history(self, conversation_id: str, msg_id: int | None = None,
                           count: int = 20):
        """IDLMessage/listMessagesPagination: (cid, nil, int64 mid, count).

        mid = 锚点消息 ID (从该消息向历史方向翻页);
        首次调用用 get_chat_info_unlimited/feed_chats 拿最新 mid, 之后用响应 f2(nextTs 锚)/末条 mid.
        响应 {1:[msg信封], 2:next_anchor, 3:hasMore}; 免费版 0 → 130050 权益期外."""
        body = pagination_body(conversation_id, msg_id, count)
        return await self.client.request("/r/IDLMessage/listMessagesPagination", body, profile="warm")

    async def get_message(self, conversation_id: str, msg_id: int):
        """IDLMessage/getMessageByIdWithCidV2: {1:cid, 2:mid} → 单条完整消息."""
        body = get_msg_by_id_with_cid_body(conversation_id, msg_id)
        return await self.client.request("/r/IDLMessage/getMessageByIdWithCidV2", body, profile="warm")

    # ---- forward / recall / genGroupId ----

    async def forward(self, msg_id: int, conversation_id: str, uuid8: str | None = None):
        """IDLSend/forward: {1:uuid8, 2:int64 mid, 3:cid} → {1:新mid, 2:ts, 3:{1:0,2:1}}.

        转发一条消息到目标会话.
        注: mid 须为 int, cid 为 str; 顺序错 → decode error:1."""
        body = pack({1: uuid8 or send_uuid(), 2: int(msg_id), 3: str(conversation_id)})
        return await self.client.request("/r/IDLSend/forward", body, profile="warm")

    async def recall_message(self, conversation_id: str, msg_id: int):
        """IDLMessage/recallMessageV2: 位置流 (fixstr(cid), int64 mid).

        超时限 mid → 400 `402041 消息已超出撤回时限，开通会员可撤回72小时内`;
        假 mid → 200 0B (静默). 免费版撤回窗口 24h; 调用方自行把握."""
        body = positional_body(str(conversation_id), int(msg_id))
        return await self.client.request("/r/IDLMessage/recallMessageV2", body, profile="warm")

    async def group_id(self, conversation_id: str):
        """IDLConversation/genGroupId: 裸 fixstr(群cid) → uint64 gid.

        仅群会话; 单聊 cid → 400."""
        return await self.client.request("/r/IDLConversation/genGroupId",
                                         _fixstr(str(conversation_id)), profile="warm")

    # ---- 空 body / 空 JSON / 空 map 即可调用的接口族 ----

    async def list_categories(self):
        """IDLCategory/listCategoriesV2: JSON 族 b'{}' → 会话分类列表."""
        return await self.client.request("/r/IDLCategory/listCategoriesV2", b"{}", profile="warm")

    async def friend_list(self):
        """FriendI/getFriendList: JSON 族 b'{}' (无企业好友时空)."""
        return await self.client.request("/r/Adaptor/FriendI/getFriendList", b"{}", profile="warm")

    async def list_devices(self):
        """LoginDeviceStatus/fetchLoginStatusDevices: 空 body → {1:[{1:did,2:ua,3:os}],2:ts} 在线设备."""
        return await self.client.request("/r/Adaptor/LoginDeviceStatus/fetchLoginStatusDevices",
                                         b"", profile="warm")

    async def space_token(self):
        """CSpace/getToken: 空 body → str16 包 JSON {"expireTime":...,"sessionId":"dtspace_u-..."}."""
        return await self.client.request("/r/Adaptor/CSpace/getToken", b"", profile="warm")

    async def white_domains(self):
        """CommonI/getWhiteDomains: 空 body → 域名白名单."""
        return await self.client.request("/r/Adaptor/CommonI/getWhiteDomains", b"", profile="warm")

    async def cookies_info(self):
        """CookiesI/getCookies: 空 body → cookie 配置集."""
        return await self.client.request("/r/Adaptor/CookiesI/getCookies", b"", profile="warm")

    async def list_built_in_commands(self):
        """SlashCommandI/listAllBuildInCommands: msgpack 空 map 0x80 → 斜杠命令表."""
        return await self.client.request("/r/Adaptor/SlashCommandI/listAllBuildInCommands",
                                         b"\x80", profile="warm")

    async def list_all_emotions(self):
        """ImgResI/getEmotionByVersions: 空 body → 全量表情包索引."""
        return await self.client.request("/r/Adaptor/ImgResI/getEmotionByVersions", b"", profile="warm")

    async def energy_emotions(self):
        """TextEmotionI/listEnergyTextEmotion: 空 body → 能量文案表情."""
        return await self.client.request("/r/Adaptor/TextEmotionI/listEnergyTextEmotion",
                                         b"", profile="warm")

    async def undo_count(self):
        """IDLOnebox/getUserUndoCount: 空 body → 用户撤销计数."""
        return await self.client.request("/r/Adaptor/IDLOnebox/getUserUndoCount", b"", profile="warm")

    async def org_summary(self):
        """CSpace/getOrgSummary: 空 body → 组织云盘摘要."""
        return await self.client.request("/r/Adaptor/CSpace/getOrgSummary", b"", profile="warm")

    async def org_info(self, org_id: int | str | None = None):
        """ContactI/getOrgInfo: 位置流 (int org_id) → 组织完整模型.

        20 键: 1=orgId, 2=org名, 5={3:工作台URL}, 7=创建者uid, 11=超时ms, 13=行业码,
        14=行业名, 17=corpId. JSON/map 形状 → idl-01 serialization error (仅位置流可用)."""
        return await self.client.request("/r/Adaptor/ContactI/getOrgInfo",
                                         positional_body(int(org_id or self.org_id or 0)),
                                         profile="warm")

    async def orchard_data(self, uid: int | str | None = None):
        """OrchardPushI/getData: 位置流 (int uid) → 工作台聚合模型.

        {1:uid, 2:ts, 3:'微应用', 4:URL, 5:[应用组], 6:{org:配置}, 14:{}}."""
        return await self.client.request("/r/Adaptor/OrchardPushI/getData",
                                         positional_body(int(uid or 0)), profile="warm")

    async def orchard_org_page(self, uid: int | str | None = None):
        """OrchardPushI/getOrgPage: 位置流 (int uid) → 组织工作台页配置."""
        return await self.client.request("/r/Adaptor/OrchardPushI/getOrgPage",
                                         positional_body(int(uid or 0)), profile="warm")

    async def nav_tabs_ex(self, conversation_id: str):
        """Adaptor/ConvNavTabI/listTab: 命名 map {1:cid} (与无前缀路由不同宿主).

        群 cid → {1:[],2:False,3:False,4:0,5:ts}; 需真实 cid (短 cid → cid not exists)."""
        return await self.client.request("/r/Adaptor/ConvNavTabI/listTab",
                                         pack({1: str(conversation_id)}), profile="warm")

    async def mail_notify_switch(self):
        """CMailI/getDingMailNotifySwitch: 任意形状 200 2B (false+\\n) — 邮件通知开关."""
        return await self.client.request("/r/Adaptor/CMailI/getDingMailNotifySwitch",
                                         b"", profile="warm")

    async def at_remind_tasks(self):
        """OAUploadI/batchPullAtRemindTask: 任意形状 200 2B (nil) — @提醒任务拉取."""
        return await self.client.request("/r/Adaptor/OAUploadI/batchPullAtRemindTask",
                                         b"", profile="warm")

    async def red_point_community(self):
        """WorkspaceRedPointI/queryRedPointCountForPcCommunity: 空 body → nil. 社区红点."""
        return await self.client.request("/r/Adaptor/WorkspaceRedPointI/"
                                         "queryRedPointCountForPcCommunity", b"", profile="warm")

    # ---- 搜索 ----

    async def search_user(self, keyword: str):
        """UserMixI/searchUserProfileByKeywordV3: 位置流 (fixstr keyword) → [用户].

        响应: [{1:profile深链(uidCipher), 2:昵称, 4:False, 6:False}].
        11030 = 该用户设置了不可被搜索 (隐私设置); uidCipher 是 16B 加密 uid."""
        return await self.client.request(consts.URI_SEARCH_USER,
                                         positional_body(str(keyword)), profile="warm")

    # ---- 会话模式判定 ----

    _chat_flags_cache: dict = {}  # cid → {type, subtype, member_count}

    async def _chat_flags(self, conversation_id: str) -> dict:
        cid = str(conversation_id)
        if cid in self._chat_flags_cache:
            return self._chat_flags_cache[cid]
        flags = {"type": 0, "subtype": 0, "member_count": 0}
        try:
            r = await self.client.request("/r/IDLConversation/getByIdUnlimited",
                                          _fixstr(cid), profile="warm")
            o = unpack_loose(r.body)
            conv = o.get(1) if isinstance(o, dict) else None
            if isinstance(conv, dict):
                flags = {"type": conv.get(2) or 0,     # 1=单聊 2=群
                         "subtype": conv.get(3) or 0,   # 2=普通群 等子类型
                         "member_count": 0}
            members = o.get(2) if isinstance(o, dict) else None
            if isinstance(members, list):
                flags["member_count"] = len(members)
        except Exception:
            pass
        self._chat_flags_cache[cid] = flags
        return flags

    async def is_self_chat(self, conversation_id: str) -> bool:
        """「和自己的会话」= 单聊 (type=1) 且成员仅 1 (cid 形如 uid:uid).

        查询失败一律 False — 宁可不触发也不误判."""
        cid = str(conversation_id)
        if ":" in cid:
            a, b = cid.split(":", 1)
            return a == b
        f = await self._chat_flags(cid)
        return f["type"] == 1 and f["member_count"] <= 1

    async def is_thread_chat(self, conversation_id: str) -> bool:
        """话题群判定: 群 (type=2) 且 subtype 高位标记话题模式.

        普通群 f3=1 (f2=2); 未识别的子类型先保守 False."""
        f = await self._chat_flags(conversation_id)
        return f["type"] == 2 and f["subtype"] not in (0, 1, 2, 3)

    async def is_bot(self, uid: int) -> bool:
        """机器人判定: uid > 2^46 (服务端消息里 bot 发送者的 uid 是超高位区间).

        系统号 (20991 等) uid 区间与真人 (5-10 位) 明显分离;
        get_user_profile 对 bot 返回空 nick 可作二次校验."""
        try:
            r = await self.client.request("/r/Adaptor/UserMixI/getUserProfileByUid",
                                          pack(int(uid)), profile="warm")
            o = unpack_loose(r.body)
            if isinstance(o, dict):
                nick = o.get(2)
                if nick is None or nick == b"":
                    return True
                return False
        except Exception:
            pass
        return int(uid) > 70_368_744_177_664  # 2^46 兜底

    async def get_group_name(self, conversation_id: str) -> str:
        """群名: getByIdUnlimited f4/f5 (单聊时 f6=对方 nick)."""
        try:
            r = await self.client.request("/r/IDLConversation/getByIdUnlimited",
                                          _fixstr(str(conversation_id)), profile="warm")
            o = unpack_loose(r.body)
            conv = o.get(1) if isinstance(o, dict) else None
            if isinstance(conv, dict):
                name = conv.get(4) or conv.get(5)
                if isinstance(name, bytes):
                    return name.decode("utf-8", "replace")
                if isinstance(name, str):
                    return name
                ext = conv.get(6)
                if isinstance(ext, dict):
                    t = ext.get(b"title") or ext.get("title")
                    if isinstance(t, bytes):
                        return t.decode("utf-8", "replace")
        except Exception:
            pass
        return ""

    async def create_chat(self, uid: int) -> str:
        """发起/获取与 uid 的单聊 cid.

        钉钉单聊 cid 是确定性的: sorted('小uid:大uid') — 无需服务端创建.
        send 到该 cid 时服务端自动开通会话 (getByIdUnlimited 立即可查)."""
        pair = sorted([str(self.uid or 0), str(int(uid))], key=int)
        return ":".join(pair)

    async def search_tabs(self, org_id: str | None = None, protocol_ver: int = 15):
        body = search_tabs_body(org_id or self.org_id, protocol_ver)
        return await self.client.request(consts.URI_SEARCH_TABS, body, profile="warm")

    async def search_feed(self, org_id: str | None = None, request_id: str | None = None):
        body = search_feed_body(org_id or self.org_id, request_id)
        return await self.client.request(consts.URI_SEARCH_FEED, body, profile="warm")

    async def search_hot(self, org_id: str | None = None, request_id: str | None = None):
        body = search_hot_body(org_id or self.org_id, request_id)
        return await self.client.request(consts.URI_SEARCH_HOT, body, profile="warm")

    async def corpid(self, org_id: str | None = None):
        body = corpid_body(org_id or self.org_id)
        return await self.client.request("/r/Adaptor/OpenOrgI/getCorpidByOrgid", body, profile="warm")

    async def search(self, org_id: str | None = None):
        """搜索面板四次发送, 与客户端进搜索页的顺序一致."""
        org = org_id or self.org_id
        tabs = await self.search_tabs(org)
        feed = await self.search_feed(org)
        hot = await self.search_hot(org)
        corp = await self.corpid(org)
        return {"tabs": tabs, "feed": feed, "hot": hot, "corpid": corp}

    async def start_listening(self):
        self.client.header_mode = "session"
        self.client.on_any_push = self._on_push

    def _on_push(self, topic: str, body: bytes):
        raw = body
        if body[:2] == b"\x1f\x8b":
            body = zlib.decompressobj(31).decompress(body)
        parsed = dig(unpack_loose(body))
        if topic in ("/s/sync", "/s/synca") and isinstance(parsed, dict):
            inner = parsed.get("1") or parsed.get(1)
            if isinstance(inner, dict):
                ts = inner.get("3") or inner.get(3)
                if ts:
                    loop = __import__("asyncio").get_event_loop()
                    loop.create_task(self.ack_diff(int(ts)))
        events = []
        find_events(parsed, events)
        for ev in events:
            for h in self._handlers:
                h({"topic": topic, "event": ev, "raw_len": len(raw)})


def unpack_loose(body: bytes):
    import msgpack
    try:
        return msgpack.unpackb(body, raw=True, strict_map_key=False)
    except Exception:
        pass
    if body.endswith(b"\n"):  # LWP 部分响应带尾部换行, msgpack 因多余字节拒解
        try:
            return msgpack.unpackb(body[:-1], raw=True, strict_map_key=False)
        except Exception:
            pass
    return {"raw": body.hex()}


def dig(o, depth=0):
    if isinstance(o, dict):
        return {(k.decode(errors="replace") if isinstance(k, bytes) else str(k)): dig(v, depth + 1)
                for k, v in o.items()}
    if isinstance(o, list):
        return [dig(v, depth + 1) for v in o]
    if isinstance(o, (bytes, bytearray)) and depth < 6 and len(o) >= 2:
        import msgpack
        try:
            inner = msgpack.unpackb(bytes(o), raw=True, strict_map_key=False)
            if isinstance(inner, (dict, list)):
                return dig(inner, depth + 1)
        except Exception:
            pass
        try:
            return o.decode("utf-8")
        except Exception:
            return o.hex()
    if isinstance(o, bytes):
        try:
            return o.decode("utf-8")
        except Exception:
            return o.hex()
    return o


def find_events(x, out):
    if isinstance(x, dict):
        keys = set(x.keys())
        if ("oid" in keys or "openConversationId" in keys) and len(keys) >= 2:
            out.append(x)
        for v in x.values():
            find_events(v, out)
    elif isinstance(x, list):
        for v in x:
            find_events(v, out)
