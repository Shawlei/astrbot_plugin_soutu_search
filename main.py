"""AstrBot 搜图插件（astrbot_plugin_soutu_search）

两个入口：
1. **以图搜图** → 上传图片调用 soutubot.moe 做相似检索。
2. **关键词搜图** → 调用 Safebooru DAPI 按标签检索。

触发方式：
- 被动监听：群聊/私聊中出现图片时自动搜图（受配置开关 + 冷却限制）。
- 指令触发：``/搜图``（附图 = 以图搜图；带文本 = 关键词搜图），``/搜图帮助`` 查看用法。

设计约束：任何异常都不得让插件崩溃，统一降级为友好提示；默认只回文字与来源链接，
不发送缩略图（``nsfw_send_image=False``），以规避平台风控。
"""

from __future__ import annotations

import os
import re
import time
from collections import OrderedDict
from pathlib import Path, PurePath

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.cache import TTLCache, make_image_key, make_tags_key
from .core.formatter import SourceOutcome, blocks_to_components, format_outcome
from .core.image_source import ImagePayload, ImageSource
from .core.safebooru_client import SafebooruClient
from .core.soutu_client import SoutuClient

PLUGIN_NAME = "astrbot_plugin_soutu_search"

# 指令名，**长的 / 更具体的在前**（保证 `搜图帮助x` 先匹配到 `搜图帮助`，rest 才是 `x`）；
# `/搜图帮助` 与别名单独注册，这里仅用于「是否为指令」判定与参数还原。
_COMMAND_NAMES = ("搜图帮助", "搜图help", "soutuhelp", "搜图", "找图", "soutu")

# 匹配开头的唤醒前缀（如 "/"、"!"、"。")：非 \w 且非空白，允许连续多个
_PREFIX_RE = re.compile(r"^[^\w]+")

# 访问控制模式：all=不限制（默认，向后兼容）；whitelist=仅列表内可用；blacklist=列表内禁用
_ACCESS_MODES = ("all", "whitelist", "blacklist")
# 访问控制范围：all=指令与自动搜图都受限（默认）；auto=仅自动搜图受限，指令照常响应
_ACCESS_SCOPES = ("all", "auto")

# 标识列表的字符串形态分隔符：逗号 / 空白 / 换行（用于把误写成字符串的名单拆成多项）
_ID_SPLIT_RE = re.compile(r"[,\s]+")

# 指令受限时的友好提示（仅在指令路径使用；自动搜图受限时**静默跳过**，避免群内刷屏）
ACCESS_DENIED_TEXT = "🚫 本会话未启用搜图功能，如需使用请联系管理员。"


def _strip_wake_prefix(text: str) -> str:
    """去掉开头的唤醒前缀，返回剩余文本。

    用明确的「前缀字符类」替代原先依赖 ``\\b`` 的正则边界判定——
    ``\\b`` 对「紧贴指令名」的形式（如 ``/搜图cat``、``/soutuhelp``）会失配，
    这里改为「去前缀后以指令名开头」的确定性判断，覆盖紧贴写入的用法。
    """
    return _PREFIX_RE.sub("", str(text or ""))


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


def _command_head(text: str) -> str | None:
    """若文本以本插件指令名开头，返回命中的指令名；否则返回 None。

    判定规则（在「去掉唤醒前缀」之后）：
    1. 按 ``_COMMAND_NAMES`` 顺序**最长优先**匹配指令名；
    2. 匹配到后，仅当「紧随字符」**不是**非 ASCII 字符（即非人话连读）时才认定为指令。
       覆盖紧贴写法 ``/搜图cat``、``/soutuhelp``、``搜图帮助x``，
       同时放行 ``/搜图 初音未来`` 这类空白分隔的中文关键词，
       并排除 ``搜图真有意思``、``soutubot很棒``、``找图…`` 等自然语句。
    """
    stripped = _strip_wake_prefix(text)
    for name in _COMMAND_NAMES:
        if stripped.startswith(name):
            if not _is_human_continuation(stripped[len(name):]):
                return name
    return None


HELP_TEXT = """📖 搜图插件用法

① 以图搜图：发送图片并附带 `/搜图`（也可直接发图片自动搜图）
   · 支持引用一张图片后发送 `/搜图`
   · 支持直接用 `/搜图 <图片链接>`
② 关键词搜图：`/搜图 <关键词>`，例如 `/搜图 cat_ears`
③ 自动搜图：群聊/私聊中发图会自动搜图（可在插件配置中关闭）
④ 查看帮助：`/搜图帮助`

说明：默认只回复文字与来源链接，不发送缩略图。可在插件配置中开启 `nsfw_send_image`
以附带缩略图。"""


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


def _raw_message_text(event: AstrMessageEvent) -> str:
    """获取事件的原始文本（兼容 get_message_str / message_str）。"""
    try:
        raw = event.get_message_str()
    except Exception:
        raw = getattr(event, "message_str", "") or ""
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _recover_command_args(event: AstrMessageEvent) -> str | None:
    """从原始消息中还原「指令名之后」的完整参数。

    为什么：AstrBot 的 CommandFilter 在参数带默认值时会只把第一个词传入，
    这里直接读原始消息文本兜底，即使框架行为变化也不丢参数。
    """
    raw = _raw_message_text(event)
    if not raw:
        return None
    head = _command_head(raw)
    if head is None:
        return None
    rest = _strip_wake_prefix(raw)[len(head):]
    return rest.strip()


def _is_command_message(event: AstrMessageEvent) -> bool:
    """判断消息是否以本插件指令开头（用于自动搜图去重）。

    采用「去前缀后以指令名开头」的确定性判断，能覆盖 ``/搜图cat``、``/soutuhelp``
    等紧贴写入的形式，避免框架已路由到 handler 的消息又被自动监听重复回复。
    """
    return _command_head(_raw_message_text(event)) is not None


class _RecentMessageRegistry:
    """记录最近被指令 handler 处理过的消息，避免自动监听重复回复。

    键为 ``(session_id, message_id)``。带 TTL 过期与容量上限，随时间自动清理，
    不会无界增长。
    """

    def __init__(self, maxsize: int = 4096, ttl: float = 120.0) -> None:
        self.maxsize = max(1, int(maxsize))
        self.ttl = float(ttl)
        self._store: "OrderedDict[tuple, float]" = OrderedDict()

    def _prune(self, now: float) -> None:
        """惰性清理过期项。"""
        if self.ttl > 0:
            expired = [k for k, ts in self._store.items() if (now - ts) >= self.ttl]
            for key in expired:
                self._store.pop(key, None)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)  # 淘汰最旧

    def add(self, key: tuple) -> None:
        """登记一条已处理消息。"""
        if key is None:
            return
        now = time.monotonic()
        self._store[key] = now
        self._store.move_to_end(key)
        self._prune(now)  # 写入后立即裁剪，保证不超过容量上限

    def contains(self, key: tuple) -> bool:
        """查询并消费（命中即移除，避免长期占用）。"""
        if key is None:
            return False
        now = time.monotonic()
        self._prune(now)
        return self._store.pop(key, None) is not None


class SoutuSearchPlugin(Star):
    """AstrBot 搜图插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = dict(config or {})
        self.config = cfg

        # ---- 读取配置（全部带默认值，缺配置也能跑） ----
        self.enable_auto_search = _to_bool(cfg.get("enable_auto_search"), True)
        self.nsfw_send_image = _to_bool(cfg.get("nsfw_send_image"), False)

        self.search_factor = _to_str(cfg.get("search_factor"), "1.2")
        if self.search_factor not in ("1.2", "1.4"):
            self.search_factor = "1.2"

        self.result_count = max(1, _to_int(cfg.get("result_count"), 3))
        self.min_score = max(0, _to_int(cfg.get("min_score"), 28))
        self.auto_search_cooldown = max(0, _to_int(cfg.get("auto_search_cooldown"), 30))
        self.cache_ttl = max(0, _to_int(cfg.get("cache_ttl"), 3600))
        self.request_timeout = max(5, _to_int(cfg.get("request_timeout"), 30))

        self.safebooru_rating = _to_str(cfg.get("safebooru_rating"), "safe")
        if self.safebooru_rating not in ("safe", "all"):
            self.safebooru_rating = "safe"

        self.soutu_base_url = _to_str(cfg.get("soutu_base_url"), "https://soutubot.moe")
        self.safebooru_base_url = _to_str(cfg.get("safebooru_base_url"), "https://safebooru.org")

        # 回复正文长度上限（字符），超出按字符边界截断
        self.max_reply_chars = max(0, _to_int(cfg.get("max_reply_chars"), 1200))
        # 单张图片大小上限（字节），默认 10MB
        self.max_image_bytes = max(1, _to_int(cfg.get("max_image_bytes"), 10 * 1024 * 1024))

        self.data_dir = self._resolve_data_dir()

        # ---- 组装组件（会话均为惰性创建） ----
        # 本地图片来源仅允许 AstrBot 数据目录（防任意文件读取）；具体见 README「安全」。
        self.image_source = ImageSource(
            timeout=self.request_timeout,
            allowed_roots=[self.data_dir],
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
        self.cache = TTLCache(default_ttl=self.cache_ttl)

        # 自动搜图冷却记录：会话标识 -> 上次自动搜图时间（monotonic）
        self._auto_cooldown: dict[str, float] = {}
        self._cooldown_max_entries = 4096
        # 已被指令 handler 处理的消息（(会话, 消息号)），避免自动监听重复回复
        self._handled_messages = _RecentMessageRegistry(maxsize=4096, ttl=120.0)

        logger.info(
            "[搜图] 插件已加载: 自动搜图=%s, 发缩略图=%s, 模式=%s, 结果数=%s, 冷却=%ss, "
            "正文上限=%s, 图片上限=%sB, 访问控制=%s/%s",
            self.enable_auto_search,
            self.nsfw_send_image,
            self.search_factor,
            self.result_count,
            self.auto_search_cooldown,
            self.max_reply_chars or "无",
            self.max_image_bytes,
            self._access_mode(),
            self._access_scope(),
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
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
        ):
            try:
                await obj.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[搜图] 关闭 %s 会话失败: %s", name, exc)

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #
    @filter.command("搜图", alias={"soutu", "找图"})
    async def sou_cmd(self, event: AstrMessageEvent, args: str = ""):
        """/搜图 指令入口：自动判别「关键词」还是「图片」。"""
        self._mark_handled(event)
        if not self._is_access_allowed(event, channel="command"):
            # 指令受限：回一句简短提示，让用户知道不是插件坏了
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        text = args.strip() if isinstance(args, str) else ""
        recovered = _recover_command_args(event)
        if recovered is not None and len(recovered) > len(text):
            text = recovered

        # 文本形式的帮助子指令
        if text in ("帮助", "help", "-h", "--help", "用法"):
            yield event.plain_result(HELP_TEXT)
            return

        async for result in self._dispatch_search(event, text, auto=False):
            yield result

    @filter.command("搜图帮助", alias={"搜图help", "soutuhelp"})
    async def sou_help_cmd(self, event: AstrMessageEvent):
        """/搜图帮助：输出用法说明。"""
        self._mark_handled(event)
        if not self._is_access_allowed(event, channel="command"):
            yield event.plain_result(ACCESS_DENIED_TEXT)
            return
        yield event.plain_result(HELP_TEXT)

    # ------------------------------------------------------------------ #
    # 被动监听：自动搜图
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，出现图片时自动以图搜图（受开关与冷却限制）。"""
        if not self.enable_auto_search:
            return
        # 访问控制：自动搜图受限时**静默跳过**（不回任何消息，避免群内刷屏）
        if not self._is_access_allowed(event, channel="auto"):
            logger.debug("[搜图] 会话 %s 未通过访问控制，静默跳过自动搜图", self._session_key(event))
            return
        # 优先用确定性登记机制判重：凡被指令 handler 处理过的消息一律跳过
        if self._handled_messages.contains(self._message_key(event)):
            return
        if _is_command_message(event):
            return  # 兜底：指令消息由指令处理器负责，避免重复回复

        try:
            payload = await self.image_source.from_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 自动搜图取图失败: %s", exc)
            return
        if payload is None:
            return

        session_key = self._session_key(event)
        now = time.monotonic()
        last = self._auto_cooldown.get(session_key, 0.0)
        if self.auto_search_cooldown > 0 and (now - last) < self.auto_search_cooldown:
            # 冷却期内静默跳过：自动搜图本身即用于防刷屏，不再额外回消息
            logger.debug("[搜图] 会话 %s 处于冷却期，跳过自动搜图", session_key)
            return
        self._prune_cooldowns(now)
        self._auto_cooldown[session_key] = now

        async for result in self._search_by_image(event, payload, silent_when_empty=True, cached_note=False):
            yield result

    # ------------------------------------------------------------------ #
    # 核心调度
    # ------------------------------------------------------------------ #
    async def _dispatch_search(self, event: AstrMessageEvent, text: str, *, auto: bool):
        """判别搜索类型并执行。"""
        try:
            payload = await self.image_source.from_event(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搜图] 取图失败: %s", exc)
            payload = None

        if payload is not None:
            async for result in self._search_by_image(event, payload, silent_when_empty=False, cached_note=True):
                yield result
            return

        if text:
            async for result in self._search_by_keyword(event, text):
                yield result
            return

        # 既无图片也无关键词 → 给出用法
        yield event.plain_result(HELP_TEXT)

    async def _search_by_image(
        self,
        event: AstrMessageEvent,
        payload: ImagePayload,
        *,
        silent_when_empty: bool,
        cached_note: bool,
    ):
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

        if silent_when_empty and not outcome.results:
            logger.info("[搜图] 自动搜图无命中，静默跳过")
            return

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

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """获取会话唯一标识（群/私聊），用于自动搜图冷却。"""
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                return value
        return "default"

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        """获取消息号（用于判重），取不到返回空串。"""
        message_obj = getattr(event, "message_obj", None)
        for holder in (message_obj, event):
            if holder is None:
                continue
            for attr in ("message_id", "id"):
                value = getattr(holder, attr, None)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, int):
                    return str(value)
        return ""

    def _message_key(self, event: AstrMessageEvent) -> tuple:
        """构造判重键 ``(会话, 消息标识)``。

        消息号缺失时，**不使用空串**（否则同会话内不同消息会共用空键而互相误判、静默吞消息），
        改为以**事件对象本身的 ``id()``** 作为唯一回退键（等价于「消息对象」的唯一标识）：
        同一事件对象被框架同时派发给 handler 与监听器时仍能正确判重，而不同消息彼此独立。
        """
        session = self._session_key(event)
        message_id = self._message_id(event)
        if message_id:
            return (session, message_id)
        return (session, f"obj:{id(event)}")

    def _mark_handled(self, event: AstrMessageEvent) -> None:
        """登记「已被指令 handler 处理」的消息，供自动监听判重。"""
        self._handled_messages.add(self._message_key(event))

    def _prune_cooldowns(self, now: float) -> None:
        """冷却表惰性清理：先剔过期项，仍超上限则按最旧淘汰。"""
        cooldown = self.auto_search_cooldown
        if cooldown > 0:
            expired = [k for k, ts in self._auto_cooldown.items() if (now - ts) >= cooldown]
            for key in expired:
                self._auto_cooldown.pop(key, None)
        if len(self._auto_cooldown) >= self._cooldown_max_entries:
            overflow = len(self._auto_cooldown) - self._cooldown_max_entries
            ordered = sorted(self._auto_cooldown.items(), key=lambda kv: kv[1])
            for key, _ in ordered[: overflow + 1]:
                self._auto_cooldown.pop(key, None)

    # ------------------------------------------------------------------ #
    # 访问控制（群/会话黑白名单）
    # ------------------------------------------------------------------ #
    def _access_mode(self) -> str:
        """动态读取访问控制模式（每次判定时读取，便于配置保存后尽快生效）。

        非法值一律回退 ``all``（不限制），保证容错与向后兼容。
        """
        cfg = self.config
        mode = _to_str(cfg.get("access_mode"), "all")
        return mode if mode in _ACCESS_MODES else "all"

    def _access_scope(self) -> str:
        """动态读取访问控制范围：``all``（默认，二者都受限）或 ``auto``（仅自动搜图受限）。

        非法值一律回退 ``all``。
        """
        cfg = self.config
        scope = _to_str(cfg.get("access_scope"), "all")
        return scope if scope in _ACCESS_SCOPES else "all"

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

    def _is_access_allowed(self, event: AstrMessageEvent, *, channel: str) -> bool:
        """判断当前事件是否被允许使用插件。

        Args:
            event: 消息事件。
            channel: ``"command"``（指令）或 ``"auto"``（自动搜图）。

        语义：
        - ``access_scope=auto`` 时**仅自动搜图受限**，指令永远放行；
        - ``access_mode=all``（默认）不限制，whitelist / blacklist 一律被忽略（向后兼容）；
        - ``access_mode=whitelist``：仅列表内可用；**列表为空 → fail-closed（全部拒绝）**；
        - ``access_mode=blacklist``：列表内禁用、列表外可用；**列表为空 → 放行全部**（非对称设计）。
        """
        if self._access_scope() == "auto" and channel == "command":
            return True

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
