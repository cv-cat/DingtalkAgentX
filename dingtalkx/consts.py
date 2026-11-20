"""钉钉 PC 客户端协议常量 (DingTalk 9.0.8-Release.261105001)."""

APP_KEY = "564ca050be89b14ee78d692f6ded748d"
APP_VERSION = "9.0.8-Release.261105001"
BUILD_NO = "261105001"
CHANNEL = "201200"
COMPILE_VERSION = "1771550"
GAEA_VERSION = "4.3.16-dingtalk.atlas5-SNAPSHOT"

# UA 需带 Gaea 段且与客户端版本一致, 否则头长度校验不过.
USER_AGENT = (
    f"Win/10.0 (Windows NT;zh_CN) App/{APP_VERSION} "
    f"AliApp(DingTalk/{APP_VERSION}) com.dingtalk.win/{COMPILE_VERSION} "
    f"Channel/{CHANNEL} Architecture/x86_64 Gaea/{GAEA_VERSION}"
)

# 匿名默认值: 实际运行一律由账号登录态覆盖
# (web 控制台扫码随机生成 did, 登录后 uid/nick/avatar/org 全部来自服务端返回).
UID = "0"
NICK = ""
AVATAR = ""
ORG_FALLBACK = ""

# 32 hex 设备 id. 代码里按需覆盖; 这里给个确定性占位, 避免共享样本设备指纹.
import hashlib as _hl
DID = _hl.md5(b"dingtalkx-default-device").hexdigest()
TZ = "Asia/Shanghai"
# cache-header 声明的可缓存头. 值本身的长度也要对齐: login=37, reg=49, reg_corp=105.
CACHE_LOGIN = "app-key cs-idx-ver did tz ua vhost wv"
CACHE_REG = "app-key client-glo-unit cs-idx-ver did tz ua vhost wv"
CACHE_REG_CORP = (
    "app-key app-realm-orgId client-glo-unit cs-idx-ver did "
    "dt-global-corp-id dt-global-edition tz ua vhost wv"
)

# 扫码登录二维码地址模板 (PC 客户端登录页用同一地址)
QR_LOGIN_URL = "http://qr.dingtalk.com/action/login?code="

# 设备材料: 登录流程现场生成 (auth.QrLoginFlow 传入或默认生成).
# 每次登录用随机 did + 随机票据即可, 服务端不校验历史设备.
import secrets as _sec

DEVICE_TICKET = "P1gA" + ''.join(_sec.choice(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for _ in range(80))
DEVICE_MAC = ':'.join(f"{_sec.randbelow(256):02X}" for _ in range(6))
DEVICE_FP = "AAEA" + ''.join(_sec.choice(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(300)) + "="
HISTORY_UIDS: list[str] = []
# 搜索 org 字段 (登录后从 profile org_ids 取, 这里只是 cli.py 无账号搜索时的兜底)
ORG_ID = "0"
CORP_ID = ""
# reg 帧里的索引版本, 与具体账号无关 (会话建立后服务器下发刷新值)
FC_VER = "0"
SET_VER = "0"
SYNC_HEADER = ",;;;"
VIA = "lws://0.0.0.0:443?isp=mobile&rm=na620&sni=dk&source=aladdin&static=1&"

# 长链服务器 (主)
LWS_HOST = "lws-desktop.dingtalk.com"
LWS_PORT = 443
LWS_SNI = "dk"
# 备用
LWS_EOS_HOST = "lws-eos.dingtalk.com"
TLS_HOST = "tls-desktop.dingtalk.com"
BACKUP_LWS_HOST = "backup.lws.dingtalk.com"
BACKUP_HTTP_HOST = "backup.http.dingtalk.com"

# 备用 lws 直连地址池 (带 pub-key pin)
LWS_SERVERS = [
    "lws://59.82.29.130:443?pub-key=118675f66fa3df0dbdd4418b2a2711d95593873e7f037e17515cfe64f660b512&rm=na620&sni=dk",
    "lws://203.119.169.22:443?pub-key=118675f66fa3df0dbdd4418b2a2711d95593873e7f037e17515cfe64f660b512&rm=na610&sni=dk",
    "lws://111.62.167.204:443?pub-key=118675f66fa3df0dbdd4418b2a2711d95593873e7f037e17515cfe64f660b512&rm=na620&sni=dk&static=1",
    "lws://111.63.206.39:443?pub-key=118675f66fa3df0dbdd4418b2a2711d95593873e7f037e17515cfe64f660b512&rm=na610&sni=dk",
]

VHOST = "WK"
WV = "im:3,au:3,sy:10"

# ============ RPC URIs (LWP 长链) ============
URI_ALADDIN_LOOKUP = "/r/Aladdin/lookup"
URI_REG_INIT = "/reg/init"

# 登录
URI_QR_GENERATE = "/r/Adaptor/QrcodeI/generateForLwpQrV2"
URI_QR_LOGIN_URL = "/r/Adaptor/QrcodeI/generateQrLoginUrl"
URI_QR_LOGIN_CONFIRM = "/r/Adaptor/QrcodeI/qrcodeLoginForLwp"
URI_LOGIN_GRANT = "/r/Adaptor/LoginI/grantLoginUnified"
URI_QUERY_MOBILE_STATUS = "/r/Adaptor/LoginI/queryMobileStatus"
URI_QUERY_FIDO = "/r/Adaptor/LoginI/queryFidoFactor"
URI_AUTO_LOGIN = "/r/Adaptor/PersonalDeviceI/authorizeByAutoLogin"
URI_GET_COOKIES = "/r/Adaptor/CookiesI/getCookies"

# 消息
URI_SEND_MSG = "/r/IDLSend/send"
URI_LIST_MESSAGES = "/r/IDLMessage/listMessagesPagination"
URI_CONV_LIST = "/r/IDLConversation/listNewestExtV3"
URI_CONV_GET_BY_IDS = "/r/IDLConversation/getByIds"
URI_CONV_MEMBERS = "/r/IDLConversation/listMembersV3"
URI_SYNC_GET_STATE = "/r/Sync/getState"
URI_SYNC_ACK_DIFF = "/r/Sync/ackDiff"
URI_MARK_READ = "/r/IDLMessageStatus/updateToReadV2"

# 搜索
URI_SEARCH_USER = "/r/Adaptor/UserMixI/searchUserProfileByKeywordV3"
URI_SEARCH_FRIEND = "/r/Adaptor/FriendI/searchFriend"
URI_SEARCH_TABS = "/r/Adaptor/SearchOpenI/listCategoryTabV2"
URI_SEARCH_FEED = "/r/Adaptor/SearchRpI/listSearchFeed"
URI_SEARCH_HOT = "/r/Adaptor/SearchRpI/listHotTopicV2"

# 消息扩展操作
URI_SEND_FORWARD = "/r/IDLSend/forward"
URI_SEND_PREVIEW = "/r/IDLSend/sendByPreviewProperty"
URI_SEND_THREAD_ROOT = "/r/IDLSend/convThreadRootMsgSend"
URI_REPLY = "/r/Adaptor/IDLReply/reply"
URI_RECALL = "/r/IDLMessage/recallMessage"

# 推送 topic
TOPIC_QR_SCANED = "/s/qrcode_scaned"
TOPIC_MSG = "/s/msg"
TOPIC_SYNC = "/s/sync"
TOPIC_SYCA = "/s/synca"
TOPIC_SYNC_DEV = "/s/syncDev"

HEARTBEAT_URI = "/!"
