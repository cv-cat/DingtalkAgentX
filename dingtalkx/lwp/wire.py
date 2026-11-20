"""LWP 帧编码器 — 头序/头数量/头长度/body 长度与客户端格式一致.

编码规则:
- 头按固定顺序, 不是调用方 dict 顺序
- did 在 login / reg / reg-init / 冷心跳里必带, 32 hex
- len 头 = body 字节数; body 为空则不发 len
- 字符串 >31 字节用 str16 (0xda), 不用 str8 (0xd9). 差 1 字节服务器会拒
"""
from __future__ import annotations

from .. import consts


def pack_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    n = len(raw)
    if n <= 31:
        return bytes([0xA0 | n]) + raw
    return b"\xda" + n.to_bytes(2, "big") + raw


def pack(obj) -> bytes:
    """客户端 msgpack 风格: str16 跳过 str8, map/array 保持插入序."""
    if obj is None:
        return b"\xc0"
    if isinstance(obj, bool):
        return b"\xc3" if obj else b"\xc2"
    if isinstance(obj, int):
        if obj < 0:
            if obj >= -32:
                return bytes([obj & 0xFF])
            if obj >= -128:
                return b"\xd0" + obj.to_bytes(1, "big", signed=True)
            if obj >= -32768:
                return b"\xd1" + obj.to_bytes(2, "big", signed=True)
            if obj >= -2147483648:
                return b"\xd2" + obj.to_bytes(4, "big", signed=True)
            return b"\xd3" + obj.to_bytes(8, "big", signed=True)
        if obj <= 0x7F:
            return bytes([obj])
        if obj <= 0xFF:
            return b"\xcc" + obj.to_bytes(1, "big")
        if obj <= 0xFFFF:
            return b"\xcd" + obj.to_bytes(2, "big")
        if obj <= 0xFFFFFFFF:
            return b"\xce" + obj.to_bytes(4, "big")
        return b"\xcf" + obj.to_bytes(8, "big")
    if isinstance(obj, str):
        return pack_str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return pack_str(bytes(obj).decode("utf-8"))
    if isinstance(obj, list):
        n = len(obj)
        head = bytes([0x90 | n]) if n <= 15 else b"\xdc" + n.to_bytes(2, "big")
        return head + b"".join(pack(x) for x in obj)
    if isinstance(obj, dict):
        n = len(obj)
        head = bytes([0x80 | n]) if n <= 15 else b"\xde" + n.to_bytes(2, "big")
        return head + b"".join(pack(k) + pack(v) for k, v in obj.items())
    raise TypeError(type(obj))


def pack_values(*parts) -> bytes:
    """qrcodeLoginForLwp: 5 个 msgpack 值拼接, 没有 array 头."""
    return b"".join(pack(p) for p in parts)


def start_line(uri: str) -> str:
    if uri.startswith("LWP"):
        return uri
    return f"LWP {uri}"


def encode_frame(uri: str, mid: int, headers: list[tuple[str, str]], body: bytes = b"") -> bytes:
    lines = [start_line(uri), f"mid:{mid:08x} 0"]
    for key, value in headers:
        lines.append(f"{key}:{value}")
    return ("\n".join(lines) + "\n\n").encode() + body


def _static(ctx) -> dict[str, str]:
    return {
        "app-key": consts.APP_KEY,
        "cs-idx-ver": "0",
        "did": ctx.device_id,
        "dt": "p",
        "tz": consts.TZ,
        "ua": consts.USER_AGENT,
        "vhost": consts.VHOST,
        "wv": consts.WV,
    }


def headers_for(profile: str, ctx, body: bytes) -> list[tuple[str, str]]:
    """按固定头序生成. 缺 did / 错 cache-header / len 与 body 不一致都会被拒."""
    st = _static(ctx)
    blen = len(body)
    if profile == "login":
        keys = [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_LOGIN),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("dt", st["dt"]),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("wv", st["wv"]),
            ("len", str(blen)),
        ]
        return keys
    if profile == "hb_cold":
        return [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_LOGIN),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("wv", st["wv"]),
        ]
    if profile == "hb_bare":
        return []
    if profile == "reg_init_short":
        return [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_LOGIN),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("wv", st["wv"]),
        ]
    if profile == "reg_init_full":
        return [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_LOGIN),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("fc-v2-idx-ver", ctx.fc_ver),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("via", ctx.via),
            ("wv", st["wv"]),
        ]
    if profile == "reg_small":
        return [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_REG),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("dt", st["dt"]),
            ("fc-v2-idx-ver", ctx.fc_ver),
            ("set-ver", ctx.set_ver),
            ("sync", ctx.sync_header),
            ("token", ctx.token),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("wv", st["wv"]),
            ("len", str(blen)),
        ]
    if profile == "reg_corp":
        rows = [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_REG_CORP),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("dt", st["dt"]),
            ("dt-global-corp-id", ctx.corp_id),
            ("fc-v2-idx-ver", ctx.fc_ver),
            ("set-ver", ctx.set_ver),
            ("token", ctx.token),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("via", ctx.via),
            ("wv", st["wv"]),
            ("len", str(blen)),
        ]
        return rows
    if profile == "reg_sub":
        return [
            ("app-key", st["app-key"]),
            ("cache-header", consts.CACHE_REG_CORP),
            ("cs-idx-ver", st["cs-idx-ver"]),
            ("did", st["did"]),
            ("dt", st["dt"]),
            ("dt-global-corp-id", ctx.corp_id),
            ("fc-v2-idx-ver", ctx.fc_ver),
            ("subscribe", "false"),
            ("token", ctx.token),
            ("tz", st["tz"]),
            ("ua", st["ua"]),
            ("vhost", st["vhost"]),
            ("via", ctx.via),
            ("wv", st["wv"]),
            ("len", str(blen)),
        ]
    if profile == "warm":
        rows = [("dt", st["dt"])]
        if blen:
            rows.append(("len", str(blen)))
        return rows
    raise KeyError(profile)


def profile_for(uri: str, mode: str) -> str:
    path = uri[4:].strip() if uri.startswith("LWP") else uri
    if path in ("/!",):
        return "hb_bare" if mode == "session" else "hb_cold"
    if path == "/reg/init":
        return "reg_init_full" if mode == "session" else "reg_init_short"
    if path in (consts.URI_QR_GENERATE, consts.URI_QR_LOGIN_CONFIRM):
        return "login"
    if path == "/reg":
        return "reg_corp" if mode == "session" else "reg_small"
    return "warm"


# 可用的 RPC 目录. 头型按连接阶段.
# 登录页: hb_cold / reg_init_short / login
# 登录后业务: warm (mid+dt+len). /reg 用 reg_small 或 reg_corp.
RPC_URIS = (
    "/!",
    "/reg/init",
    "/reg",
    "/r/Adaptor/QrcodeI/generateForLwpQrV2",
    "/r/Adaptor/QrcodeI/qrcodeLoginForLwp",
    "/r/Adaptor/FriendI/getFriendList",
    "/r/IDLConversation/getByIds",
    "/r/IDLCategory/listCategoriesV2",
    "/r/Adaptor/Setting/getConf",
    "/r/Sync/getState",
    "/r/AliasI/queryAll",
    "/r/FollowI/listAll",
    "/r/Adaptor/CSpace/getToken",
    "/r/Adaptor/AssetTagMixI/listTags",
    "/r/Adaptor/CommonI/getWhiteDomains",
    "/r/Adaptor/IotUserTerminalI/uploadDevInfo",
    "/r/Adaptor/ApDeviceI/getKeyAndSsidsV2",
    "/r/Adaptor/IDLDing/pullDingListOrderBySendAt",
    "/r/Adaptor/IDLDing/getConfirmStatusInfo",
    " /r/Adaptor/IDLCalendar/batchGetEventTodos",
    "/r/Adaptor/OAUploadI/batchPullAtRemindTask",
    "/r/Adaptor/TextEmotionI/listCustomTextEmotion",
    "/r/Adaptor/TextEmotionI/listOrgTextEmotion",
    "/r/Adaptor/TextEmotionI/listRecommendedTextEmotion",
    "/r/Adaptor/TextEmotionI/listEnergyTextEmotion",
    "/r/Adaptor/CMailI/getLoginEmailInfosV2",
    "/r/Adaptor/OrchardPushI/getData",
    "/r/Adaptor/SafeGuardDispatcher/recvStrategyInfoEx",
    "/r/Adaptor/OpenEncryptI/getEncryptBoxStatus",
    "/r/Adaptor/HomeTabI/queryHomeTabConfigs",
    "/r/Adaptor/DingTalkCollabPortalI/getBadges",
    "/r/Adaptor/DingtalkPersonalV2I/getBadges",
    "/r/Adaptor/LoginI/grantLoginUnified",
    "/r/Adaptor/PersonalDeviceI/authorizeByAutoLogin",
    "/r/LogI/batchLog",
    "/r/Adaptor/LoginI/queryPasskey",
    "/r/Adaptor/SafeI/reportAfterProcessStart",
    "/r/Adaptor/EnterpriseRealmI/getGlobalConfigByMulVersion",
    "/r/Adaptor/VariableModuleI/getPCAllVariableModule",
    "/r/Adaptor/IDLRemoteDeviceLock/getSelfLockStatus",
    "/r/Adaptor/UserMixI/getUserProfileExtensionByUid",
    "/r/Adaptor/OnePunchLive/getLiveFloatingWindows",
    "/r/Adaptor/WorkspaceRedPointI/queryRedPoint",
    "/r/IDLConversation/listNewestExtV3",
    "/r/Adaptor/CCNoticeI/currentCircleRedPoint",
    "/r/Adaptor/IDLOnebox/getUserUndoCount",
    "/r/Adaptor/TaskBadgeI/getTaskBadgeInfo",
    "/r/Adaptor/DingTalkTodo/getTodoBadgeInfo",
    "/r/Adaptor/IDLDingMsg/getDingBadgeInfo",
    "/r/Adaptor/IDLDing/getSecondTabBadgeInfo",
    "/r/Adaptor/IDLCalendar/listCalendarNewestV2",
    "/r/Sync/ackDiff",
    "/r/Adaptor/EnterpriseRealmI/injectRealmLWPHeadersAfterReg",
    "/r/Adaptor/UserMixI/getUserProfileByUids",
    "/r/Adaptor/CsConfigI/getConf",
    "/r/IDLConversation/getByIdUnlimited",
    "/r/Adaptor/ToolbarI/getMsgFilterToolbar",
    "/r/Adaptor/CookiesI/getCookies",
    "/r/Adaptor/CMailI/getDingMailNotifySwitch",
    "/r/Adaptor/CloudContactI/getPreferBizCallNum",
    "/r/CloudSettingI/updateCloudSettings",
    "/r/Adaptor/CallAdminI/getAdContent",
    "/r/Adaptor/Setting/updateSetting",
    "/r/Adaptor/StartupRecommendGuideI/getStartupRecommendGuideScene",
    "/r/Adaptor/ImgResI/getRecommendEmotionByVersion",
    "/r/Adaptor/ImgResI/getEmotionByVersions",
    "/r/Adaptor/SearchOpenI/listCategoryTabV2",
    "/r/Adaptor/SearchRpI/listSearchFeed",
    "/r/Adaptor/OpenOrgI/getCorpidByOrgid",
    "/r/Adaptor/SearchRpI/listHotTopicV2",
    "/r/Adaptor/WorkbenchI/getHomeModel",
    "/r/Adaptor/WorkSearchI/normalRecommendApp",
    "/r/Adaptor/ContactI/getOrgInfo",
    "/r/Adaptor/PublicityI/orgInGray",
    "/r/Adaptor/TodoForOneI/listTodayTodoForWorkbench",
    "/r/Adaptor/SceneContextI/getContextData",
    "/r/Adaptor/WorkSearchI/aiRecommendApp",
    "/r/Adaptor/ResourceSyncI/report",
    "/r/Adaptor/IDLCalendar/listAllFolders",
    "/r/Adaptor/IDLCalendar/listNonRepeatCalendarV2",
    "/r/Adaptor/LogHubUploadI/commitLogs",
    "/r/IDLCustomizedSingleChat/genCustomizedSingleChatCids",
    "/r/Adaptor/AIAgentI/getOrCreateMyAgentCid",
    "/r/Adaptor/LoginDeviceStatus/fetchLoginStatusDevices",
    "/r/Adaptor/TangleNetworkSession/heartbeat",
    "/r/Adaptor/OrchardPushI/getOrgPage",
    "/r/Adaptor/WorkbenchI/getBadges",
    "/r/Adaptor/WolaiStandardI/getBadges",
    "/r/Adaptor/DingtalkWolaiI/getSpaceRedPoints",
    "/r/Adaptor/CSpace/getOrgSummary",
    "/r/Adaptor/UserMixI/getUserProfileByUid",
    "/r/Adaptor/UserMixI/getMyUserProfileExtension",
    "/r/Adaptor/EnterpriseRealmI/pushGlobalByMulVersion",
    "/r/Adaptor/ToolbarI/getMsgFilterToolbarUnionModel",
    "/r/Adaptor/DingTalkTodo/getTodoBadgeInfoV2",
    "/r/Adaptor/DtWaveCardI/listMyInteractiveCards",
    "/r/IDLCategory/listConversationsByCategoryV2",
    "/r/Adaptor/IDLConfigurableInfoQuery/queryTargetMenuInfo",
    "/r/Adaptor/CSpaceSync/genDeviceId",
    "/r/Adaptor/CSpaceSync/monitor",
    "/r/Adaptor/CloudContactI/getCurCallConfig",
    "/r/Adaptor/SlashCommandI/listAllBuildInCommands",
    "/r/IDLConvThread/listNewestUserConvThreads",
    "/r/Adaptor/MiniAppI/getMiniAppMetaData",
    "/r/Adaptor/ResourceSyncI/init",
    "/r/IDLMessageStatus/updateToViewV2",
    "/r/IDLConversation/listMembersV3",
    "/r/Adaptor/ConversationSafeI/getFirstEnterConversationMsgs",
    "/r/Adaptor/ContactI/getUserEmployeeInfos",
    "/r/Adaptor/ToolbarI/getPcConversationToolbarWithSlide",
    "/r/Adaptor/TangleNetworkContainer/getContainerContent",
    "/r/IDLMessage/listMessagesPagination",
    "/r/FileDown/get",
    "/r/Adaptor/ContactGuideI/getContactGuideInviteRecommend",
    "/r/Adaptor/ConvNavTabI/listTab",
    "/r/IDLSend/send",
    "/r/Adaptor/ClientSyncI/report",
    "/r/Adaptor/CSpace/checkConvEnableSaveToOrg",
    "/r/Adaptor/ContactGuideI/getOrgMemAddRmdResidentGuide",
    "/r/Adaptor/WorkspaceRedPointI/queryRedPointCountForPcCommunity",
    "/r/Adaptor/ContactGuideI/getBottomBannerGuide",
    "/r/Adaptor/DeviceStatus/subscribe",
    "/r/Adaptor/IDLOnebox/listNewest",
    "/r/Adaptor/DtWaveTemplateI/syncTemplateStatus",
)
