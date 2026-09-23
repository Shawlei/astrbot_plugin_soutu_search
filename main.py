"""AstrBot 搜图插件（astrbot_plugin_soutu_search）

三种能力，**各自独立指令、互斥触发**（不再由一个指令自动判别）：
1. **以图搜本子** → ``<前缀>搜本``（别名 ``搜本子`` / ``soutu`` / ``找图``）上传图片调用
   soutubot.moe（搜图Bot酱）做相似检索。**只接受图片**（消息图片 / 引用图片 / 图片直链）。
2. **关键词搜图** → ``<前缀>搜图`` 调用 Safebooru DAPI 按标签检索。**只接受关键词**，
   明确**不接受图片**（收到图片 / 图片直链时回引导提示，不下载、不搜索）。
3. **搜 P 站（SauceNAO 反查）** → ``<前缀>搜P站``（别名 ``pixiv`` / ``saucenao``）上传图片
   调用 SauceNAO，可限定 Pixiv 库反查出处/画师（需配置 API Key）。

帮助指令：``搜本帮助`` / ``搜图帮助`` / ``搜P站帮助``（各有英文别名，见 ``_COMMAND_NAMES``）。

触发方式（**仅指令触发**）：
- 前缀跟随 AstrBot 全局配置的命令前缀（顶层 ``wake_prefix``，默认 ``/``）。
- **不监听消息、不会自动搜图**：只有显式发送指令才会触发，杜绝被动打扰。

设计约束：任何异常都不得让插件崩溃，统一降级为友好提示；默认只回文字与来源链接，
不发送缩略图（``nsfw_send_image=False``），以规避平台风控。
"""

from __future__ import annotations

import importlib
import re
import time
from pathlib import Path, PurePath

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.cache import TTLCache, make_image_key, make_saucenao_key, make_tags_key
from .core.formatter import SourceOutcome, blocks_to_components, format_outcome
from .core.image_source import ImagePayload, ImageSource, is_http_url
from .core.safebooru_client import SafebooruClient
from .core.saucenao_client import (
    SaucenaoClient,
    resolve_db_mask,
    resolve_hide,
    resolve_min_similarity,
)
from .core.soutu_client import SoutuClient

PLUGIN_NAME = "astrbot_plugin_soutu_search"

# 指令名，**长的 / 更具体的在前**，且**帮助类必须排在各自本体之前**。
# 注意「搜本子」必须排在「搜本」之前：否则「搜本子」会被「搜本」抢先匹配，
# rest="子" 是 CJK → 被判为人话连读 → 指令失效。
_COMMAND_NAMES = (
    # 帮助类必须排在各自本体之前
    "搜本帮助", "搜本help", "soutuhelp",
    "搜图帮助", "搜图help",
    "搜P站帮助", "搜P站help", "saucenaohelp",
    # "搜本子" 必须排在 "搜本" 之前（否则 "搜本子" 会被 "搜本" 抢先匹配，
    # rest="子" 是 CJK → 被判为人话连读 → 指令失效）
    "搜本子",
    "搜P站", "saucenao", "pixiv",
    "搜本", "soutu", "找图",
    "搜图",
)

# 默认命令前缀（AstrBot 全局配置顶层 wake_prefix 的兜底值）
DEFAULT_WAKE_PREFIX = "/"

# 通用唤醒前缀兜底正则：匹配开头的非 \w 且非空白字符（如 "/"、"!"、"。")，允许连续多个
_PREFIX_RE = re.compile(r"^[^\w]+")

# 命中配置前缀后的「重复前缀清理」正则：贪婪吃掉剩余开头**连续的非单词、非空白**字符，
# 以还原旧版对 `//搜图`、`##搜图` 的兜底（刻意不吃空白，保留前缀与指令间的分隔空格）。
_REPEAT_PREFIX_RE = re.compile(r"^[^\w\s]+")

# 访问控制模式：all=不限制（默认，向后兼容）；whitelist=仅列表内可用；blacklist=列表内禁用
_ACCESS_MODES = ("all", "whitelist", "blacklist")

# 标识列表的字符串形态分隔符：逗号 / 空白 / 换行（用于把误写成字符串的名单拆成多项）
_ID_SPLIT_RE = re.compile(r"[,\s]+")

# 指令受限时的友好提示
ACCESS_DENIED_TEXT = "🚫 本会话未启用搜图功能，如需使用请联系管理员。"

# SauceNAO 未配置 API Key 时的引导（**发请求前**就提示，避免无谓配额消耗）
SAUCENAO_KEY_MISSING_TEXT = (
    "⚠️ 尚未配置 SauceNAO API Key，无法使用「搜P站」。\n"
    "请在插件配置中填写 `saucenao_api_key`，申请地址："
    "https://saucenao.com/user.php?page=search-api"
)

# SauceNAO 未附图时的用法提示
SAUCENAO_NO_IMAGE_TEXT = (
    "🖼 请发送一张图片并附带指令，或引用一张图片后再发送指令。\n"
    "示例：发送图片 + `{p}搜P站`，或直接 `{p}搜P站 <图片链接>`。"
)

# 图片直链获取失败时的提示（文案宣称支持 `<指令> <图片链接>`，故必须真正处理失败分支）
IMAGE_URL_FETCH_FAIL_TEXT = (
    "😥 无法获取图片链接：{err}\n"
    "请确认它是可直接访问的图片直链（支持 http/https，且非内网地址），或改用「引用图片」的方式。"
)

# 「搜本」收到纯关键词时的引导（soutubot 只支持以图搜图）
BOOK_KEYWORD_NOT_SUPPORTED_TEXT = (
    "📕 「搜本」是**以图搜本子**（搜图Bot酱），只支持图片，不支持关键词。\n"
    "· 关键词搜图请用 `{p}搜图 <关键词>`\n"
    "· 反查 P 站出处请用 `{p}搜P站`"
)

# 「搜图」收到图片/图片链接时的引导（搜图只做关键词）
IMAGE_NOT_SUPPORTED_TEXT = (
    "🔍 「搜图」只做**关键词搜图**（Safebooru），不接受图片。\n"
    "· 以图搜本子请用 `{p}搜本`\n"
    "· 反查 P 站出处请用 `{p}搜P站`"
)


def _normalize_prefixes(raw) -> list[str]:
    """把 AstrBot 全局配置的命令前缀（``wake_prefix``）归一化为非空字符串列表。

    - ``str``：非空则视为**单前缀**；空白串 → 空列表；
    - ``list`` / ``tuple`` / ``set``：逐项保留非空字符串，**跳过** ``None`` / 数字等非字符串项；
    - 其它类型（``int`` / ``dict`` / ``bool`` / ``None`` 等）→ 空列表。
    """
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [item for item in raw if isinstance(item, str) and item.strip()]
    return []


def _strip_wake_prefix(text: str, prefixes=None) -> str:
    """去掉开头的命令前缀，返回剩余文本。

    - **优先按配置前缀**（``prefixes``，可为多个）用 ``str.startswith`` 逐个剥离：
      刻意**不把前缀拼进正则**，避免前缀含正则元字符（``[``、``.``、``(`` 等）时被曲解。
    - 命中配置前缀后，**再对剩余文本应用一次** ``^[^\\w\\s]+``（贪婪），
      以吃光**连续重复的前缀**（如 ``//搜图``、``##搜图``），保持与旧版一致的行为；
      该清理**只吃非空白字符**，故 ``.( 搜图`` 这类「前缀 + 分隔空格」不受影响。
    - 配置前缀都没匹配上时，**回退**通用正则 ``^[^\\w]+``（覆盖 ``/``、``!``、``。`` 等）。
    """
    text = str(text or "")
    for prefix in prefixes or []:
        if isinstance(prefix, str) and prefix and text.startswith(prefix):
            remainder = text[len(prefix):]
            # 贪婪吃掉剩余开头连续的非单词、非空白字符（重复前缀），
            # 从而不再丢失旧版「//搜图 → 搜图」的兜底能力。
            return _REPEAT_PREFIX_RE.sub("", remainder)
    return _PREFIX_RE.sub("", text)


def _is_human_continuation(rest: str) -> bool:
    """判断指令名之后的 ``rest`` 是否为「紧贴的人话连读」。

    只看**紧跟指令名的那个字符**（不含分隔空白后的参数）：
    - 若紧跟的是空白 / 结尾 / ASCII 字符 → 是正常指令写法（返回 False）；
    - 若紧跟的是 **非 ASCII 字符**（CJK 表意文字/假名、CJK 标点、``…`` 等）→ 视为人话连读（返回 True）。

    这样 ``搜图真有意思``、``soutubot很棒``、``找图…``、``搜图帮助…`` 判为非指令，
    而 ``/搜图 初音未来``、``/搜图  双空格中文`` 等「空白 + 中文参数」仍是合法指令。
    """
    if not rest:
        return False
    if rest[0].isspace():
        return False  # 空白后跟参数（含中文关键词）→ 正常指令
    attached = rest.split(maxsplit=1)[0]  # 紧贴到下一个空白为止的片段
    return not attached.isascii()


def _command_head(text: str, prefixes=None) -> str | None:
    """若文本以本插件指令名开头，返回命中的指令名；否则返回 None。

    判定规则（在「去掉命令前缀」之后）：
    1. 按 ``_COMMAND_NAMES`` 顺序**最长优先**匹配指令名；
    2. 匹配到后，仅当「紧随字符」**不是**非 ASCII 字符（即非人话连读）时才认定为指令。
       覆盖紧贴写法 ``/搜图cat``、``/soutuhelp``、``搜图帮助x``，
       同时放行 ``/搜图 初音未来`` 这类空白分隔的中文关键词，
       并排除 ``搜图真有意思``、``soutubot很棒``、``找图…`` 等自然语句。

    Args:
        text: 原始消息文本。
        prefixes: 配置的命令前缀列表（可选）；为空时回退通用正则前缀。
    """
    stripped = _strip_wake_prefix(text, prefixes)
    for name in _COMMAND_NAMES:
        if stripped.startswith(name):
            if not _is_human_continuation(stripped[len(name):]):
                return name
    return None


def _prefix_or_default(prefix) -> str:
    """帮助 / 引导文案使用的命令前缀：非法或为空时回退 ``DEFAULT_WAKE_PREFIX``。"""
    return prefix if isinstance(prefix, str) and prefix else DEFAULT_WAKE_PREFIX


_BOOK_HELP_TEMPLATE = """📖 搜本 · 以图搜本子（搜图Bot酱）用法

① 发送图片并附带 `{p}搜本`
   · 支持引用一张图片后发送 `{p}搜本`
   · 支持直接用 `{p}搜本 <图片链接>`
② 别名：`搜本子`、`soutu`、`找图`
③ 查看帮助：`{p}搜本帮助`

说明：
· 搜图Bot酱**只支持以图搜图**，不支持关键词 —— 关键词请用 `{p}搜图`
· 命中源以 nhentai / e-hentai 等本子库为主
· 默认只回复文字与来源链接，不发送缩略图（可在配置中开启 `nsfw_send_image`）"""


def _render_book_help(prefix: str) -> str:
    """按**实际命令前缀**渲染「搜本」帮助文案；前缀为空时回退 ``/``。"""
    return _BOOK_HELP_TEMPLATE.format(p=_prefix_or_default(prefix))


# 默认（前缀为 ``/``）的「搜本」帮助文案常量，便于外部引用/测试
BOOK_HELP_TEXT = _render_book_help(DEFAULT_WAKE_PREFIX)


def _render_book_keyword_hint(prefix: str) -> str:
    """渲染「搜本」收到**纯关键词**时的引导（soutubot 只支持以图搜图）。"""
    return BOOK_KEYWORD_NOT_SUPPORTED_TEXT.format(p=_prefix_or_default(prefix))


def _render_image_not_supported_hint(prefix: str) -> str:
    """渲染「搜图」收到**图片 / 图片链接**时的引导（搜图只做关键词搜图）。"""
    return IMAGE_NOT_SUPPORTED_TEXT.format(p=_prefix_or_default(prefix))


_HELP_TEMPLATE = """📖 搜图 · 关键词搜图（Safebooru）用法

① 关键词搜图：`{p}搜图 <关键词>`，例如 `{p}搜图 cat_ears`
② 查看帮助：`{p}搜图帮助`

说明：
· 「搜图」只做**关键词搜图**，不接受图片
· 以图搜本子请用 `{p}搜本`；反查 P 站出处请用 `{p}搜P站`
· 搜图**只能通过指令触发**，群内有人发图不会自动搜图
· 默认只回复文字与来源链接，不发送缩略图"""


def _render_help(prefix: str) -> str:
    """按**实际命令前缀**渲染「搜图」帮助文案；前缀为空时回退 ``/``。"""
    return _HELP_TEMPLATE.format(p=_prefix_or_default(prefix))


# 默认（前缀为 ``/``）的「搜图」帮助文案常量，便于外部引用/测试
HELP_TEXT = _render_help(DEFAULT_WAKE_PREFIX)


_SAUCENAO_HELP_TEMPLATE = """📖 搜 P 站（SauceNAO 反查）用法

① 发送图片并附带 `{p}搜P站`（也支持引用一张图片后发送，或 `{p}搜P站 <图片链接>`）
② 别名：`pixiv`、`saucenao`（如 `{p}pixiv`、`{p}saucenao`）
③ 查看帮助：`{p}搜P站帮助`

说明：
· 需先在插件配置中填写 `saucenao_api_key`（申请：https://saucenao.com/user.php?page=search-api）
· 默认只检索 **Pixiv 系列**数据库，可用配置 `saucenao_db_mask` 调整（96=仅 Pixiv）
· 免费账户配额：**150 次/天、4 次/30 秒**
· 中国大陆访问 SauceNAO 通常需要代理（AstrBot 有全局 `http_proxy` 配置可用）
· 以图搜本子请用 `{p}搜本`；关键词搜图请用 `{p}搜图`"""


def _render_saucenao_help(prefix: str) -> str:
    """按实际命令前缀渲染「搜 P 站」帮助文案；前缀为空时回退 ``/``。"""
    return _SAUCENAO_HELP_TEMPLATE.format(p=_prefix_or_default(prefix))


# 默认（前缀为 ``/``）的「搜 P 站」帮助文案常量
SAUCENAO_HELP_TEXT = _render_saucenao_help(DEFAULT_WAKE_PREFIX)


def _to_bool(value, default: bool) -> bool:
    """宽松地把配置值转为 bool。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "是", "开", "开启")
    return default


def _to_int(value, default: int) -> int:
    """宽松地把配置值转为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_str(value, default: str) -> str:
    """宽松地把配置值转为非空 str。"""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _normalize_root_entry(entry) -> Path | None:
    """把「额外允许根目录」列表中的**单项**归一化为 ``Path``；无效项返回 ``None``。

    只接受 ``str`` 或 ``pathlib.PurePath``（``Path`` 是其子类，自动覆盖）：
    - 其它类型（``int`` / ``bool`` / ``None`` / ``dict`` / ``MagicMock`` 等）一律视为非法，
      返回 ``None``（由调用方告警并跳过）。
    - 空白字符串（``""`` / ``"   "``）同样视为非法（避免把空路径当成根目录）。

    刻意**不接受任意 ``os.PathLike``**：``unittest.mock.MagicMock`` 也满足 ``isinstance(x, os.PathLike)``，
    若不加区分会引入伪路径（与 ``_resolve_data_dir`` 的历史坑一致）。
    """
    if isinstance(entry, PurePath):
        text = str(entry).strip()
    elif isinstance(entry, str):
        text = entry.strip()
    else:
        return None
    if not text:
        return None
    try:
        return Path(text)
    except Exception:  # noqa: BLE001 - 极少数平台的非法路径字符
        return None


def _to_path_list(value) -> list[Path]:
    """解析「额外允许根目录」配置为 ``list[Path]``（仅保留合法项并告警）。

    该配置在 schema 中是 ``type: "list"``，因此**顶层必须是序列**：

    - ``None`` → 空列表；
    - ``list`` / ``tuple`` / ``set`` → 逐项处理（嵌套序列递归展开）；
    - 其它类型（单个 ``str`` / ``int`` / ``dict`` 等）→ **打 ``logger.warning`` 后按空处理**
      （不静默吞配置错误，也不把裸字符串误当成路径）。

    每个条目走 :func:`_normalize_root_entry`：只接受 ``str`` / ``pathlib.PurePath``
    （``Path`` 为其子类），其余类型忽略并告警。
    """
    roots: list[Path] = []
    if value is None:
        return roots
    if not isinstance(value, (list, tuple, set)):
        logger.warning(
            "[搜图] extra_allowed_roots 配置类型异常（应为 list），已按空处理: %s",
            type(value).__name__,
        )
        return roots

    for entry in value:
        if isinstance(entry, (list, tuple, set)):
            roots.extend(_to_path_list(entry))
            continue
        path = _normalize_root_entry(entry)
        if path is None:
            logger.warning("[搜图] extra_allowed_roots 项类型非法已忽略: %r", entry)
            continue
        roots.append(path)
    return roots


def _raw_message_text(event: AstrMessageEvent) -> str:
    """获取事件的原始文本（兼容 get_message_str / message_str）。"""
    try:
        raw = event.get_message_str()
    except Exception:
        raw = getattr(event, "message_str", "") or ""
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _recover_command_args(event: AstrMessageEvent, prefixes=None) -> str | None:
    """从原始消息中还原「指令名之后」的完整参数。

    为什么：AstrBot 的 CommandFilter 在参数带默认值时会只把第一个词传入，
    这里直接读原始消息文本兜底，即使框架行为变化也不丢参数。
    """
    raw = _raw_message_text(event)
    if not raw:
        return None
    head = _command_head(raw, prefixes)
    if head is None:
        return None
    rest = _strip_wake_prefix(raw, prefixes)[len(head):]
    return rest.strip()


class SoutuSearchPlugin(Star):
    """AstrBot 搜图插件（仅指令触发）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = dict(config or {})
        self.config = cfg

        # ---- 读取配置（全部带默认值，缺配置也能跑） ----
        self.nsfw_send_image = _to_bool(cfg.get("nsfw_send_image"), False)

        self.search_factor = _to_str(cfg.get("search_factor"), "1.2")
        if self.search_factor not in ("1.2", "1.4"):
            self.search_factor = "1.2"

        self.result_count = max(1, _to_int(cfg.get("result_count"), 3))
        self.min_score = max(0, _to_int(cfg.get("min_score"), 28))
        self.cache_ttl = max(0, _to_int(cfg.get("cache_ttl"), 3600))
        self.request_timeout = max(5, _to_int(cfg.get("request_timeout"), 30))

        self.safebooru_rating = _to_str(cfg.get("safebooru_rating"), "safe")
        if self.safebooru_rating not in ("safe", "all"):
            self.safebooru_rating = "safe"

        self.soutu_base_url = _to_str(cfg.get("soutu_base_url"), "https://soutubot.moe")
        self.safebooru_base_url = _to_str(cfg.get("safebooru_base_url"), "https://safebooru.org")

        # ---- SauceNAO（搜 P 站）配置（全部带默认值，非法值回退安全默认）----
        # API Key 允许为空串（未配置时由指令入口给出引导，不发起请求）
        self.saucenao_api_key = _to_str(cfg.get("saucenao_api_key"), "")
        self.saucenao_base_url = _to_str(cfg.get("saucenao_base_url"), "https://saucenao.com")
        self.saucenao_db_mask = resolve_db_mask(cfg.get("saucenao_db_mask"))
        self.saucenao_min_similarity = resolve_min_similarity(cfg.get("saucenao_min_similarity"))
        self.saucenao_hide = resolve_hide(cfg.get("saucenao_hide"))

        # 回复正文长度上限（字符），超出按字符边界截断
        self.max_reply_chars = max(0, _to_int(cfg.get("max_reply_chars"), 1200))
        # 单张图片大小上限（字节），默认 10MB
        self.max_image_bytes = max(1, _to_int(cfg.get("max_image_bytes"), 10 * 1024 * 1024))

        self.data_dir = self._resolve_data_dir()

        # ---- 组装组件（会话均为惰性创建） ----
        # 本地图片来源白名单（防任意文件读取，遵循**最小授权**，具体见 README「安全」）：
        # 1) 插件自身数据目录（保留原行为）；
        # 2) AstrBot 的 `data/temp` 目录 —— AstrBot 收到图片后先落盘到该临时目录，再把
        #    本地路径塞进 Image.file；不放行会导致「发图搜图」永远被拒（线上 Bug）；
        # 3) 用户手动追加的 `extra_allowed_roots`（应对版本差异导致的临时目录位置变化）。
        # 注意：**绝不**放行整个 AstrBot `data/` 目录（其中含各插件配置，可能含密钥）。
        self.extra_allowed_roots = _to_path_list(cfg.get("extra_allowed_roots"))
        self.allowed_roots = self._build_allowed_roots()
        self.image_source = ImageSource(
            timeout=self.request_timeout,
            allowed_roots=self.allowed_roots,
            max_image_bytes=self.max_image_bytes,
        )
        self.soutu = SoutuClient(
            base_url=self.soutu_base_url,
            factor=self.search_factor,
            timeout=self.request_timeout,
            min_score=float(self.min_score),
        )
        self.booru = SafebooruClient(
            base_url=self.safebooru_base_url,
            timeout=self.request_timeout,
            rating=self.safebooru_rating,
        )
        self.saucenao = SaucenaoClient(
            base_url=self.saucenao_base_url,
            api_key=self.saucenao_api_key,
            db_mask=self.saucenao_db_mask,
            numres=max(6, min(40, self.result_count * 5)),
            min_similarity=self.saucenao_min_similarity,
            hide=self.saucenao_hide,
            timeout=self.request_timeout,
        )
        self.cache = TTLCache(default_ttl=self.cache_ttl)

        logger.info(
            "[搜图] 插件已加载: 发缩略图=%s, 模式=%s, 结果数=%s, 正文上限=%s, 图片上限=%sB, 访问控制=%s",
            self.nsfw_send_image,
            self.search_factor,
            self.result_count,
            self.max_reply_chars or "无",
            self.max_image_bytes,
            self._access_mode(),
        )
        # 本地图片允许根目录对排查「越权/漏授权」极为关键，加载时明确打印一次
        logger.info(
            "[搜图] 本地图片允许根目录: %s",
            "; ".join(str(root) for root in self.allowed_roots) or "（空：拒绝一切本地文件）",
        )
        logger.info(
            "[搜图] SauceNAO(搜P站): base=%s, dbmask=%s, minsim=%s, hide=%s, key=%s",
            self.saucenao_base_url,
            self.saucenao_db_mask,
            self.saucenao_min_similarity,
            self.saucenao_hide,
            "已配置" if self.saucenao_api_key else "未配置",
        )

    # ------------------------------------------------------------------ #
    # 命令前缀（跟随 AstrBot 全局配置）
    # ------------------------------------------------------------------ #
    def _wake_prefixes(self) -> list[str]:
        """读取 AstrBot 全局配置的命令前缀（顶层 ``wake_prefix``）。

        数据来源：``self.context.get_config()`` 返回的配置 dict 的**顶层** ``wake_prefix``
        字段（``list`` 类型，默认 ``["/"]``）。

        .. warning::
           ``provider_settings`` 里也有同名 ``wake_prefix``（``str``，供 LLM 服务使用），
           **不是**命令前缀；这里只读顶层那一个。

        容错：``get_config()`` 抛异常 / 返回非 dict / 字段缺失或非法 → 返回空列表，
        由调用方回退到通用正则前缀；绝不抛异常。
        """
        try:
            astrbot_config = self.context.get_config()
        except Exception as exc:  # noqa: BLE001 - 兼容缺 get_config 的环境/桩
            logger.debug("[搜图] 读取 AstrBot 配置失败，回退默认前缀: %s", exc)
            return []
        if not isinstance(astrbot_config, dict):
            return []
        return _normalize_prefixes(astrbot_config.get("wake_prefix"))

    def _help_prefix(self) -> str:
        """帮助文案使用的命令前缀：取配置前缀的第一个，缺省 ``/``。"""
        prefixes = self._wake_prefixes()
        return prefixes[0] if prefixes else DEFAULT_WAKE_PREFIX

    def _help_text(self) -> str:
        """按当前实际前缀渲染「搜图」帮助文案。"""
        return _render_help(self._help_prefix())

    def _book_help_text(self) -> str:
        """按当前实际前缀渲染「搜本」帮助文案。"""
        return _render_book_help(self._help_prefix())

    def _book_keyword_hint(self) -> str:
        """按当前实际前缀渲染「搜本」收到纯关键词时的引导。"""
        return _render_book_keyword_hint(self._help_prefix())

    def _image_not_supported_hint(self) -> str:
        """按当前实际前缀渲染「搜图」收到图片时的引导。"""
        return _render_image_not_supported_hint(self._help_prefix())

    def _saucenao_help_text(self) -> str:
        """按当前实际前缀渲染「搜 P 站」帮助文案。"""
        return _render_saucenao_help(self._help_prefix())

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @staticmethod
    def _coerce_dir(value) -> Path | None:
        """把候选目录值（``str`` / ``pathlib.PurePath``）归一化为 ``Path``；无效返回 ``None``。

        刻意只认 ``str`` / ``pathlib``（``Path`` 为其子类），不接受任意 ``os.PathLike`` 与
        ``MagicMock`` 等伪路径（原因同 :meth:`_resolve_data_dir`）。
        """
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, PurePath):
            text = str(value).strip()
        else:
            return None
        if not text:
            return None
        return Path(text)

    @staticmethod
    def _data_root_from_plugin_dir(plugin_dir: Path) -> Path | None:
        """从插件数据目录**向上回溯**，返回名为 ``data`` 的那一级；找不到返回 ``None``。

        AstrBot 的插件数据目录通常形如 ``<AstrBot>/data/plugin_data/<plugin>``，因此回溯
        父目录即可定位 ``<AstrBot>/data``。若插件数据目录本身就叫 ``data``，也能命中。
        """
        try:
            current = Path(plugin_dir).resolve()
        except Exception:  # noqa: BLE001
            current = Path(plugin_dir)
        for candidate in (current, *current.parents):
            if candidate.name == "data":
                return candidate
        return None

    def _resolve_astrbot_data_root(self) -> Path | None:
        """尽力确定 **AstrBot 的 data 根目录**（用于放行其 ``temp`` 图片目录）。

        探测顺序（**不硬编码任何绝对路径**）：
        1. 尝试 AstrBot 官方可能提供的 API（``astrbot.core.utils.astrbot_path.get_astrbot_data_path``
           等），不同版本模块/函数名可能不同，用 ``ImportError`` 兜住；拿到就用。
        2. 回退：从插件自身数据目录（``StarTools.get_data_dir``）向上回溯，找到名为 ``data``
           的那一级。
        """
        for mod_name, attr_name in (
            ("astrbot.core.utils.astrbot_path", "get_astrbot_data_path"),
            ("astrbot.core.utils.astrbot_path", "get_astrbot_path"),
            ("astrbot.core.config", "get_astrbot_data_path"),
        ):
            try:
                module = importlib.import_module(mod_name)
            except ImportError:
                continue
            except Exception as exc:  # noqa: BLE001 - 兼容极端导入错误
                logger.debug("[搜图] 导入 %s 失败: %s", mod_name, exc)
                continue
            func = getattr(module, attr_name, None)
            if not callable(func):
                continue
            try:
                value = func()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[搜图] 调用 %s.%s 失败: %s", mod_name, attr_name, exc)
                continue
            path = self._coerce_dir(value)
            if path is not None:
                return path
        return self._data_root_from_plugin_dir(self.data_dir)

    def _build_allowed_roots(self) -> list[Path]:
        """组装本地图片「允许根目录」列表（去重、保持最小授权）。

        组成（按顺序）：
        1. **插件自身数据目录**（保留原有放行范围）；
        2. **AstrBot ``data/temp`` 目录**（**仅 temp 子目录**，不含整个 ``data/``）——
           覆盖 ``data/temp/media_image_*.jpg`` 这类 AstrBot 下载落盘的临时图片；
        3. 用户配置的 ``extra_allowed_roots``。
        """
        roots: list[Path] = []

        def _add(candidate) -> None:
            if candidate is None:
                return
            try:
                resolved = Path(candidate).resolve()
            except Exception:  # noqa: BLE001
                resolved = Path(candidate).absolute()
            if resolved not in roots:
                roots.append(resolved)

        _add(self.data_dir)
        astrbot_data = self._resolve_astrbot_data_root()
        if astrbot_data is not None:
            _add(astrbot_data / "temp")  # 仅放行 temp，保持最小授权
        else:
            logger.debug("[搜图] 未能确定 AstrBot data 根目录，跳过 temp 放行")
        for extra in self.extra_allowed_roots:
            _add(extra)
        return roots

    def _resolve_data_dir(self) -> Path:
        """确定数据目录：优先 AstrBot 提供的 data 目录，失败回退插件内 data/。

        候选值**仅接受 ``str`` 或 ``pathlib`` 路径对象**，刻意**不接受任意 ``os.PathLike``**：
        ``unittest.mock.MagicMock`` 恰好也满足 ``isinstance(x, os.PathLike)``，若不加区分地
        ``Path(candidate).mkdir()``，会在工作目录下制造出 ``MagicMock/mock/<id>`` 之类的垃圾目录
        （历史上测试桩即因此污染工作区）。无法判定为真实路径时**降级**到插件内 data/，
        而不是盲目建目录；真实 AstrBot 返回的绝对路径字符串不受影响。
        """
        candidate = None
        try:
            candidate = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001 - 兼容不同 AstrBot 版本
            logger.debug("[搜图] 获取 data 目录失败，改用插件内目录: %s", exc)

        # 只认 str / pathlib 路径对象；其余（含 MagicMock 等伪 PathLike）一律视为无效
        text = ""
        if isinstance(candidate, str):
            text = candidate.strip()
        elif isinstance(candidate, PurePath):
            text = str(candidate).strip()

        if text:
            path = Path(text)
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except Exception as exc:  # noqa: BLE001
                logger.warning("[搜图] 创建数据目录失败，改用插件内目录: %s", exc)
        elif candidate is not None:
            logger.debug("[搜图] data 目录候选值非真实路径，改用插件内目录: %r", type(candidate).__name__)

        path = Path(__file__).parent / "data"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 创建数据目录失败: %s", exc)
        return path

    async def terminate(self):
        """插件卸载时清理资源。"""
        for name, obj in (
            ("image_source", self.image_source),
            ("soutu", self.soutu),
            ("booru", self.booru),
            ("saucenao", self.saucenao),
        ):
            try:
                await obj.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[搜图] 关闭 %s 会话失败: %s", name, exc)

    # ------------------------------------------------------------------ #
    # 指令入口（仅指令触发；不监听消息、不自动搜图）
    # ------------------------------------------------------------------ #
    @filter.command("搜本", alias={"搜本子", "soutu", "找图"})
    async def book_cmd(self, event: AstrMessageEvent, args: str = ""):
        """「搜本」指令入口：以图搜本子（soutubot），**只接受图片**。

        图片 / 引用图片 / 图片直链 → 走 soutubot；纯关键词 → 引导改用「搜图」。
        """
        if not self._is_access_allowed(event):
            # 指令受限：回一句简短提示，让用户知道不是插件坏了
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        text = args.strip() if isinstance(args, str) else ""
        recovered = _recover_command_args(event, self._wake_prefixes())
        if recovered is not None and len(recovered) > len(text):
            text = recovered

        # 文本形式的帮助子指令
        if text in ("帮助", "help", "-h", "--help", "用法"):
            yield event.plain_result(self._book_help_text())
            return

        async for result in self._dispatch_book(event, text):
            yield result

    @filter.command("搜本帮助", alias={"搜本help", "soutuhelp"})
    async def book_help_cmd(self, event: AstrMessageEvent):
        """「搜本帮助」指令：输出以图搜本子用法说明。"""
        if not self._is_access_allowed(event):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        yield event.plain_result(self._book_help_text())

    @filter.command("搜图")
    async def sou_cmd(self, event: AstrMessageEvent, args: str = ""):
        """「搜图」指令入口：Safebooru 关键词搜图，**只接受关键词**（不接受图片）。"""
        if not self._is_access_allowed(event):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        text = args.strip() if isinstance(args, str) else ""
        recovered = _recover_command_args(event, self._wake_prefixes())
        if recovered is not None and len(recovered) > len(text):
            text = recovered

        # 文本形式的帮助子指令
        if text in ("帮助", "help", "-h", "--help", "用法"):
            yield event.plain_result(self._help_text())
            return

        async for result in self._dispatch_search(event, text):
            yield result

    @filter.command("搜图帮助", alias={"搜图help"})
    async def sou_help_cmd(self, event: AstrMessageEvent):
        """「搜图帮助」指令：输出关键词搜图用法说明。"""
        if not self._is_access_allowed(event):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        yield event.plain_result(self._help_text())

    @filter.command("搜P站", alias={"pixiv", "saucenao"})
    async def pixiv_cmd(self, event: AstrMessageEvent, args: str = ""):
        """搜 P 站指令入口：用 SauceNAO 反查图片出处（默认仅 Pixiv 库）。

        与「搜图」/「搜本」**分开**独立指令：SauceNAO 免费配额仅 4 次/30 秒，若挂在其它
        指令上并跑会瞬间耗尽，故按需单独触发。
        """
        if not self._is_access_allowed(event):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        if not self.saucenao_api_key:
            # 未配置 API Key：发请求前就给出引导，避免无谓的网络/配额消耗
            yield event.plain_result(SAUCENAO_KEY_MISSING_TEXT)
            return
        text = args.strip() if isinstance(args, str) else ""
        recovered = _recover_command_args(event, self._wake_prefixes())
        if recovered is not None and len(recovered) > len(text):
            text = recovered
        async for result in self._dispatch_saucenao(event, text):
            yield result

    @filter.command("搜P站帮助", alias={"搜P站help", "saucenaohelp"})
    async def pixiv_help_cmd(self, event: AstrMessageEvent):
        """搜 P 站帮助指令：输出用法说明。"""
        if not self._is_access_allowed(event):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        yield event.plain_result(self._saucenao_help_text())

    # ------------------------------------------------------------------ #
    # 核心调度
    # ------------------------------------------------------------------ #
    async def _dispatch_book(self, event: AstrMessageEvent, text: str):
        """「搜本」调度：以图搜本子（soutubot），**只接受图片**。

        优先级：消息里的图片 → 文本若是 http(s) 图片直链则下载该图 → 否则（纯关键词）
        回引导提示（**不发起任何搜索**）→ 既无图也无参数则回「搜本帮助」。
        """
        try:
            payload = await self.image_source.from_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 取图失败: %s", exc)
            payload = None

        if payload is not None:
            async for result in self._search_by_image(event, payload, cached_note=True):
                yield result
            return

        if text and is_http_url(text):
            try:
                payload = await self.image_source.from_source(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[搜图] 图片链接获取失败: %s", exc)
                yield event.plain_result(IMAGE_URL_FETCH_FAIL_TEXT.format(err=exc))
                return
            if payload is not None:
                async for result in self._search_by_image(event, payload, cached_note=True):
                    yield result
                return

        if text:
            # 纯关键词：soutubot 只支持以图搜图 → 给出改用「搜图」的引导，不搜索
            yield event.plain_result(self._book_keyword_hint())
            return

        # 既无图片也无参数 → 给出用法
        yield event.plain_result(self._book_help_text())

    async def _dispatch_search(self, event: AstrMessageEvent, text: str):
        """「搜图」调度：Safebooru 关键词搜图，**只接受关键词**。

        收到图片（消息图片 / 图片直链）时回引导提示，**不下载、不搜索**（避免浪费带宽、
        收敛 SSRF 面，也避免把图片路径与关键词路径混淆）。
        """
        if self.image_source.has_image(event):
            yield event.plain_result(self._image_not_supported_hint())
            return

        if text and is_http_url(text):
            # 图片直链同样视为「图片」：先于下载判定，直接引导，绝不发起请求
            yield event.plain_result(self._image_not_supported_hint())
            return

        if text:
            async for result in self._search_by_keyword(event, text):
                yield result
            return

        # 既无图片也无关键词 → 给出用法
        yield event.plain_result(self._help_text())

    async def _search_by_image(self, event: AstrMessageEvent, payload: ImagePayload, *, cached_note: bool = True):
        """以图搜图并回复。"""
        cache_key = make_image_key(payload.data)
        started = time.perf_counter()
        outcome: SourceOutcome | None = self.cache.get(cache_key)
        note = ""

        if outcome is not None:
            if cached_note:
                note = "（缓存）"
        else:
            try:
                outcome = await self.soutu.search(
                    payload.data,
                    filename=payload.filename,
                    mime=payload.mime,
                    factor=self.search_factor,
                    top_k=max(self.result_count * 5, 25),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[搜图] 以图搜图失败: %s", exc, exc_info=True)
                yield event.plain_result(
                    f"😥 以图搜图失败：{exc}\n请稍后重试，或检查网络与接口可用性。"
                )
                return
            self.cache.set(cache_key, outcome)

        elapsed = time.perf_counter() - started

        header = (
            f"🔍 以图搜图完成{note}，命中 {len(outcome.results)} 条"
            f"（相似度≥{self.min_score}），耗时 {elapsed:.2f}s"
        )
        blocks = format_outcome(
            outcome,
            nsfw_send_image=self.nsfw_send_image,
            max_results=self.result_count,
            header=header,
            max_chars=self.max_reply_chars,
        )
        yield self._emit(event, blocks)

    async def _search_by_keyword(self, event: AstrMessageEvent, keyword: str):
        """关键词搜图并回复。"""
        cache_key = make_tags_key(
            keyword,
            rating=self.safebooru_rating,
            limit=self.result_count,
            page=0,
        )
        started = time.perf_counter()
        outcome: SourceOutcome | None = self.cache.get(cache_key)
        note = ""

        if outcome is not None:
            note = "（缓存）"
        else:
            try:
                outcome = await self.booru.search_by_tags(
                    keyword,
                    limit=max(self.result_count * 3, 12),
                    page=0,
                    rating=self.safebooru_rating,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[搜图] 关键词搜图失败: %s", exc, exc_info=True)
                yield event.plain_result(
                    f"😥 关键词搜图失败：{exc}\n请稍后重试，或检查网络与接口可用性。"
                )
                return
            self.cache.set(cache_key, outcome)

        elapsed = time.perf_counter() - started

        header = (
            f"🔍 关键词「{keyword}」搜图完成{note}，命中 {len(outcome.results)} 条"
            f"，耗时 {elapsed:.2f}s"
        )
        blocks = format_outcome(
            outcome,
            nsfw_send_image=self.nsfw_send_image,
            max_results=self.result_count,
            header=header,
            max_chars=self.max_reply_chars,
        )
        yield self._emit(event, blocks)

    # ------------------------------------------------------------------ #
    # SauceNAO（搜 P 站）调度
    # ------------------------------------------------------------------ #
    async def _dispatch_saucenao(self, event: AstrMessageEvent, text: str = ""):
        """SauceNAO 反查调度：帮助子指令 → 取图（消息图片 / http 图片直链）→ 反查。"""
        if text in ("帮助", "help", "-h", "--help", "用法"):
            yield event.plain_result(self._saucenao_help_text())
            return

        try:
            payload = await self.image_source.from_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 取图失败（SauceNAO）: %s", exc)
            payload = None

        # 帮助文案宣称支持 `<指令> <图片链接>`，这里真正落实该路径（含 SSRF 防护）
        if payload is None and is_http_url(text):
            try:
                payload = await self.image_source.from_source(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[搜图] 图片链接获取失败（SauceNAO）: %s", exc)
                yield event.plain_result(IMAGE_URL_FETCH_FAIL_TEXT.format(err=exc))
                return

        if payload is None:
            yield event.plain_result(SAUCENAO_NO_IMAGE_TEXT.format(p=self._help_prefix()))
            return

        async for result in self._search_by_saucenao_image(event, payload):
            yield result

    async def _search_by_saucenao_image(self, event: AstrMessageEvent, payload: ImagePayload):
        """SauceNAO 反查并回复（独立缓存命名空间，避免与 soutu 结果互相污染）。"""
        cache_key = make_saucenao_key(payload.data)
        started = time.perf_counter()
        outcome: SourceOutcome | None = self.cache.get(cache_key)
        note = ""

        if outcome is not None:
            note = "（缓存）"
        else:
            try:
                outcome = await self.saucenao.search(
                    payload.data,
                    filename=payload.filename,
                    mime=payload.mime,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[搜图] SauceNAO 反查失败: %s", exc, exc_info=True)
                yield event.plain_result(
                    f"😥 搜P站失败：{exc}\n"
                    "请稍后重试；中国大陆访问 SauceNAO 通常需要代理（可在 AstrBot 全局配置 http_proxy）。"
                )
                return
            self.cache.set(cache_key, outcome)

        elapsed = time.perf_counter() - started

        header = (
            f"🔍 搜P站（SauceNAO）完成{note}，命中 {len(outcome.results)} 条"
            f"（相似度≥{self.saucenao_min_similarity}），耗时 {elapsed:.2f}s"
        )
        blocks = format_outcome(
            outcome,
            nsfw_send_image=self.nsfw_send_image,
            max_results=self.result_count,
            header=header,
            max_chars=self.max_reply_chars,
        )
        yield self._emit(event, blocks)

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def _emit(self, event: AstrMessageEvent, blocks: list[dict]):
        """把消息块安全地转换为 AstrBot 结果，失败降级为纯文本。"""
        try:
            components = blocks_to_components(blocks)
            if components:
                return event.chain_result(components)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 构造消息链失败，降级为纯文本: %s", exc)

        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return event.plain_result(text or "😥 结果格式化失败。")

    # ------------------------------------------------------------------ #
    # 访问控制（群/会话黑白名单，仅作用于指令通道）
    # ------------------------------------------------------------------ #
    def _access_mode(self) -> str:
        """动态读取访问控制模式（每次判定时读取，便于配置保存后尽快生效）。

        非法值一律回退 ``all``（不限制），保证容错与向后兼容。
        """
        cfg = self.config
        mode = _to_str(cfg.get("access_mode"), "all")
        return mode if mode in _ACCESS_MODES else "all"

    @staticmethod
    def _normalize_id(entry) -> str | None:
        """把**单个**标识值归一化为非空字符串；无效项返回 ``None``（表示跳过）。

        刻意跳过的"空值"：
        - ``None``：不是标识；
        - 布尔值（``True`` / ``False``）：``str()`` 会得到 ``"True"``/``"False"``，都不是合法会话 ID；
        - 数值 ``0``（``0`` / ``0.0``）：不是合法会话 ID。

        其余统一 ``str(entry).strip()``，``strip`` 后为空串的也跳过。这样可保证
        ``whitelist=[None]`` 归一化为**空集合**从而 fail-closed，而不会因 ``str(None)=="None"``
        误命中 ``umo=="None"``。
        """
        if entry is None or isinstance(entry, bool):
            return None
        if isinstance(entry, (int, float)) and entry == 0:
            return None
        text = str(entry).strip()
        return text or None

    @staticmethod
    def _as_id_set(raw) -> set[str]:
        """把标识列表配置规范化为集合（字符串化 + strip + 忽略无效项）。

        三种输入形态，兼顾容错与安全：
        - ``list`` / ``tuple`` / ``set``：逐项归一化（嵌套序列递归展开；嵌套字符串同样按分隔符拆分）；
        - ``str``：按**逗号 / 空白 / 换行**分割为多元素（``"123456, 789012"`` → 两项，
          ``"123456"`` → 一项）。这样常见的"误写成字符串"仍能按用户本意生效，
          尤其避免 ``blacklist`` 被静默丢弃而导致的 **fail-open**（危险方向）；
        - 其它类型（``int`` / ``dict`` / ``float`` 等）：**打 ``logger.warning`` 明确提示类型错误**，
          再按空集合处理——**不再静默吞掉配置错误**。
        """
        items: set[str] = set()
        if raw is None:
            return items
        if isinstance(raw, str):
            for token in _ID_SPLIT_RE.split(raw):
                norm = SoutuSearchPlugin._normalize_id(token)
                if norm is not None:
                    items.add(norm)
            return items
        if isinstance(raw, (list, tuple, set)):
            for entry in raw:
                if isinstance(entry, (list, tuple, set, str)):
                    items |= SoutuSearchPlugin._as_id_set(entry)
                    continue
                norm = SoutuSearchPlugin._normalize_id(entry)
                if norm is not None:
                    items.add(norm)
            return items
        logger.warning(
            "[搜图] 访问控制名单配置类型异常（应为 list 或 str），已按空处理: %s",
            type(raw).__name__,
        )
        return items

    def _whitelist(self) -> set[str]:
        """动态读取白名单（每次判定时读取）。"""
        cfg = self.config
        return self._as_id_set(cfg.get("whitelist"))

    def _blacklist(self) -> set[str]:
        """动态读取黑名单（每次判定时读取）。"""
        cfg = self.config
        return self._as_id_set(cfg.get("blacklist"))

    @staticmethod
    def _collect_id(value, bucket: set[str]) -> None:
        """把候选标识值规范化后放入集合：递归展开序列、归一化、丢弃无效项。

        与名单侧共用 ``_normalize_id``，保证候选与名单的归一化规则**完全对称**
        （同样的 ``None`` / 布尔 / ``0`` / 空串都会被跳过，避免两侧口径不一致）。
        """
        if isinstance(value, (list, tuple, set)):
            for item in value:
                SoutuSearchPlugin._collect_id(item, bucket)
            return
        norm = SoutuSearchPlugin._normalize_id(value)
        if norm is not None:
            bucket.add(norm)

    def _candidate_ids(self, event: AstrMessageEvent) -> set[str]:
        """构造事件的**候选会话标识集合**（任一命中列表即视为命中）。

        覆盖三种常见填法，便于用户按需填写：
        - ``event.unified_msg_origin``：会话唯一 ID（umo）
        - ``event.message_obj.group_id``（及 ``event.group_id``）：群号（私聊通常为空 → 跳过）
        - **发送者 ID**（尽力获取，私聊场景下便于「按人限制」；取不到则跳过、不报错）
        """
        candidates: set[str] = set()
        if event is None:
            return candidates

        self._collect_id(getattr(event, "unified_msg_origin", None), candidates)
        message_obj = getattr(event, "message_obj", None)
        self._collect_id(getattr(message_obj, "group_id", None), candidates)
        self._collect_id(getattr(event, "group_id", None), candidates)

        # 发送者 ID：多来源尽力获取，任一路径可用即可
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                self._collect_id(getter(), candidates)
            except Exception:  # noqa: BLE001 - 尽力而为，取不到就算了
                pass
        for holder in (event, message_obj):
            if holder is None:
                continue
            self._collect_id(getattr(holder, "sender_id", None), candidates)
            self._collect_id(getattr(holder, "user_id", None), candidates)
            sender = getattr(holder, "sender", None)
            if sender is not None:
                self._collect_id(getattr(sender, "user_id", None), candidates)
                self._collect_id(getattr(sender, "sender_id", None), candidates)
                self._collect_id(getattr(sender, "id", None), candidates)
        return candidates

    def _is_access_allowed(self, event: AstrMessageEvent) -> bool:
        """判断当前指令事件是否被允许使用插件（访问控制仅作用于指令通道）。

        语义：
        - ``access_mode=all``（默认）不限制，whitelist / blacklist 一律被忽略（向后兼容）；
        - ``access_mode=whitelist``：仅列表内可用；**列表为空 → fail-closed（全部拒绝）**；
        - ``access_mode=blacklist``：列表内禁用、列表外可用；**列表为空 → 放行全部**（非对称设计）。
        """
        mode = self._access_mode()
        if mode == "all":
            return True

        candidates = self._candidate_ids(event)
        if mode == "whitelist":
            allowed = self._whitelist()
            if not allowed:
                return False  # fail-closed：白名单为空 → 谁都不放行
            return bool(candidates & allowed)

        # mode == "blacklist"
        denied = self._blacklist()
        if not denied:
            return True  # 黑名单为空 = 不限制
        return not bool(candidates & denied)
