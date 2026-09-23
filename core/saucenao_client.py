"""SauceNAO（saucenao.com）以图反查 provider。

用途：把一张图片反查到**原始出处 / 作者**，尤其适合「专门搜 P 站（Pixiv）」——
通过 ``dbmask`` 位掩码限定位掩码把检索范围锁定到 Pixiv 系列数据库。

接口（依据 ``recon/API_SAUCENAO.md`` 规范，与本机连通性无关，纯按规范实现）::

    POST https://saucenao.com/search.php      # multipart 上传本地图片
    query: api_key / output_type=2 / dbmask / numres / minsim / hide
    form : file（图片二进制）
    headers: 浏览器 UA + Referer: https://saucenao.com/

.. warning::
   **本机（开发环境）连不通 saucenao.com（TLS 连接重置）**，故本模块的解析逻辑以
   **构造的响应样例**做单测，未做真实联网端到端验证；真实连通性请在用户机器上用
   ``tests/live_saucenao_check.py`` 复验。中国大陆通常需要代理（AstrBot 有全局 ``http_proxy``）。

解析容错要点（均为线上真实会踩的坑）：
- ``results[].header.similarity`` 是**字符串**（如 ``"95.42"``），须安全转 float，非法值不崩；
- ``header.status != 0`` 视为错误，给出可读信息（不静默返回空）；
- ``results`` 为 ``[]`` / 缺失 / 非 list → 优雅返回无结果；
- ``data`` 结构**因库而异**（Pixiv 有 ``pixiv_id``/``member``；booru 类有 ``source``/``material``；
  书籍类有 ``part``/``year``），解析按可用字段回退，不假设某字段一定存在；
- 链接回退链：``data.ext_urls[0]`` → 由 ``pixiv_id`` 拼 ``https://www.pixiv.net/artworks/{id}``
  → ``data.source`` → 无则标记为无链接；
- 缩略图 ``thumbnail`` 带 ``auth``/``exp`` 签名会过期，**不外传文本**，仅交给上层「先下载再发」。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from astrbot.api import logger

from .formatter import SearchResult, SourceOutcome

DEFAULT_BASE_URL = "https://saucenao.com"

# --------------------------------------------------------------------------- #
# 数据库位掩码（来自官方说明：按十六进制相加后转十进制）
# --------------------------------------------------------------------------- #
DB_H_MAGS = 0x1  # #0  h-mags
DB_HCG = 0x4  # #2  hcg
DB_PIXIV = 0x20  # #5  pixiv
DB_PIXIV_HISTORICAL = 0x40  # #6  pixivhistorical
DB_SEIGA = 0x100  # #8  seiga_illust（NicoNico 静画）
DB_DANBOORU = 0x200  # #9  danbooru
DB_YANDERE = 0x1000  # #12 yande.re
DB_FAKKU = 0x10000  # #16 FAKKU
DB_H_MISC_NHENTAI = 0x20000  # #18 H-MISC (nhentai)
DB_GELBOORU = 0x1000000  # #25 gelbooru
DB_KONACHAN = 0x2000000  # #26 konachan
DB_H_MISC_EHENTAI = 0x2000000000  # #38 H-Misc (ehentai)
DB_TWITTER = 0x10000000000  # #41 Twitter
DB_SKEB = 0x80000000000  # #44 Skeb

# 仅 Pixiv + PixivHistorical —— 作为「**非法值回退**」的**保守默认**（见下方说明）。
#
# ⚠️ 注意区分两个「默认」：
# 1. **schema 默认**（用户未改动时实际生效）：``SCHEMA_DEFAULT_DB_MASK = 0`` =
#    全部索引（不发送 dbmask，服务端按 #999 全部处理）。这是**推荐值**：
#    pixiv(#5) 对近期图常无命中，而 pixivhistorical(#6) 是老图快照库，锁死在 96
#    会让用户「只搜出 2018 年以前的老图」。
# 2. **非法值回退**（用户填了负数 / 非整数时）：保守回退到 ``0x60``(96) 这个
#    「Pixiv 限定」。**刻意不退回 ``0``** —— ``0`` 在语义上是「全部」，若把非法值静默
#    变成「搜全部」会放大部分用户的意外行为；回退到保守的 Pixiv 限定更可控。
DEFAULT_DB_MASK = DB_PIXIV | DB_PIXIV_HISTORICAL  # == 0x60 == 96（非法值回退用）

# schema / 配置的**推荐默认**：0 = 全部索引（不发送 dbmask 参数）
SCHEMA_DEFAULT_DB_MASK = 0

DEFAULT_MIN_SIMILARITY = 50  # SauceNAO 相似度体系 0-100（独立于 soutubot 的 min_score）
DEFAULT_HIDE = 0  # 0=全显示 1=隐藏预期 R18 2=隐藏预期可疑 3=只留安全
DEFAULT_NUMRES = 15  # 返回条数（1-40）
MAX_NUMRES = 40
HIDE_CHOICES = (0, 1, 2, 3)

# index_id -> 库名（用于 index_name 缺失时回退）
SAUCENAO_INDEX_NAMES: dict[int, str] = {
    0: "h-mags",
    2: "hcg",
    5: "pixiv",
    6: "pixivhistorical",
    8: "seiga_illust",
    9: "danbooru",
    12: "yande.re",
    16: "FAKKU",
    18: "H-MISC (nhentai)",
    25: "gelbooru",
    26: "konachan",
    38: "H-Misc (ehentai)",
    41: "Twitter",
    44: "Skeb",
}

# 常见库掩码示例（供配置 hint / README 引用，避免各处重复字面量）
DB_MASK_EXAMPLES: dict[str, int] = {
    "pixiv": DB_PIXIV,
    "pixivhistorical": DB_PIXIV_HISTORICAL,
    "danbooru": DB_DANBOORU,
    "yande.re": DB_YANDERE,
    "Twitter": DB_TWITTER,
}


# --------------------------------------------------------------------------- #
# 宽松类型转换
# --------------------------------------------------------------------------- #
def _coerce_int(value: Any) -> int | None:
    """把值宽松地转成 ``int``；无法可靠转换时返回 ``None``。

    - ``bool`` 视为无效（``True`` 不是合法数字，避免被当成 1）；
    - ``int`` 原样返回；
    - 整数值的 ``float``（如 ``96.0``）接受；非整数值（``96.5``）返回 ``None``；
    - 字符串：先按 Python 字面量（支持 ``0x60`` / ``0b..`` / ``0o..``）解析，再退化为十进制；
    - 其它类型（``None`` / ``dict`` / ``list``）返回 ``None``。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 0)
        except ValueError:
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _to_float(value: Any, default: float | None = None) -> float | None:
    """宽松地把值转为 ``float``；失败返回默认值。

    用于解析 ``similarity``（**字符串** ``"95.42"``）、以及容忍非法/缺失值不崩。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_str(*values: Any) -> str:
    """返回第一个「非空字符串」（strip 后非空）；都没有则返回空串。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _id_str(value: Any) -> str:
    """把 id（``int`` / ``str``）转成无小数点、无空白的字符串；无效返回空串。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value.strip()
    return ""


# --------------------------------------------------------------------------- #
# 配置解析（非法值一律回退安全默认）
# --------------------------------------------------------------------------- #
def resolve_db_mask(value: Any, default: int = DEFAULT_DB_MASK) -> int:
    """归一化 ``db_mask`` 配置。

    - 合法非负整数（含 ``"96"`` / ``"0x60"`` / ``96.0``）→ 原值；
    - ``0`` 合法（表示不限库，搜全部）；
    - 负数 / 布尔 / 非整数 / 非数值字符串 / ``None`` → 回退 ``default``
      （默认 ``96``，即**保守的 Pixiv 限定**）。

    .. note::
       非法值**刻意不退回 ``0``**：``0`` 在语义上是「全部索引」，若把非法值静默变成
       「搜全部」会放大部分用户的意外行为。故非法值回退到 ``0x60``(96) 这个保守值；
       真正「搜全部」请显式配置 ``saucenao_db_mask=0``（也是 schema 的推荐默认）。
    """
    parsed = _coerce_int(value)
    if parsed is None or parsed < 0:
        return default
    return parsed


def resolve_min_similarity(value: Any, default: int = DEFAULT_MIN_SIMILARITY) -> int:
    """归一化 ``min_similarity``：**越界（<0 或 >100）或非法 → 回退 ``default``**。"""
    parsed = _coerce_int(value)
    if parsed is None or not 0 <= parsed <= 100:
        return default
    return parsed


def resolve_hide(value: Any, default: int = DEFAULT_HIDE) -> int:
    """归一化 ``hide``（内容过滤）：**不在 0-3 或非法 → 回退 ``default``**。"""
    parsed = _coerce_int(value)
    if parsed is None or parsed not in HIDE_CHOICES:
        return default
    return parsed


def resolve_numres(value: Any, default: int = DEFAULT_NUMRES) -> int:
    """归一化 ``numres``（返回条数 1-40）：非法或越界则裁剪/回退。"""
    parsed = _coerce_int(value)
    if parsed is None:
        return default
    if parsed < 1:
        return 1
    if parsed > MAX_NUMRES:
        return MAX_NUMRES
    return parsed


def describe_db_mask(mask: int) -> str:
    """把位掩码转成可读的库名列表（仅覆盖已知库，未知位以 ``bitN`` 标注）。

    便于把「96 = 仅 Pixiv」这类信息写进日志/展示里。
    """
    if mask == 0:
        return "全部库（不限，不发送 dbmask 参数）"
    names: list[str] = []
    # 直接用已知掩码常量映射（比按 index_id 推位更可靠）
    known = {
        DB_H_MAGS: "h-mags",
        DB_HCG: "hcg",
        DB_PIXIV: "pixiv",
        DB_PIXIV_HISTORICAL: "pixivhistorical",
        DB_SEIGA: "seiga_illust",
        DB_DANBOORU: "danbooru",
        DB_YANDERE: "yande.re",
        DB_FAKKU: "FAKKU",
        DB_H_MISC_NHENTAI: "H-MISC (nhentai)",
        DB_GELBOORU: "gelbooru",
        DB_KONACHAN: "konachan",
        DB_H_MISC_EHENTAI: "H-Misc (ehentai)",
        DB_TWITTER: "Twitter",
        DB_SKEB: "Skeb",
    }
    for bit, name in known.items():
        if mask & bit:
            names.append(name)
    if not names:
        return f"掩码 {mask}（未识别）"
    return f"掩码 {mask}（" + " + ".join(names) + "）"


def _status_message(status: int) -> str:
    """把 ``header.status`` 非 0 的错误码转成可读信息。"""
    return (
        f"SauceNAO 接口返回错误状态码 {status}"
        "（请检查 API Key 是否正确、是否超出配额，或稍后重试）"
    )


# --------------------------------------------------------------------------- #
# 结果构造
# --------------------------------------------------------------------------- #
def _library_name(index_id: int | None, index_name: Any) -> str:
    """命中库名：优先用响应里的 ``index_name``，缺失时按 ``index_id`` 回退。"""
    name = index_name.strip() if isinstance(index_name, str) and index_name.strip() else ""
    if name:
        return name
    if index_id is not None and index_id in SAUCENAO_INDEX_NAMES:
        return SAUCENAO_INDEX_NAMES[index_id]
    if index_id is not None:
        return f"库#{index_id}"
    return ""


def _pick_primary_url(data: dict, pixiv_id: Any, ext_urls: list[str]) -> str:
    """按回退链挑选主链接：``ext_urls[0]`` → ``pixiv_id`` 拼接 → ``data.source`` → 无。"""
    if ext_urls:
        return ext_urls[0]
    pixiv_str = _id_str(pixiv_id)
    if pixiv_str:
        return f"https://www.pixiv.net/artworks/{pixiv_str}"
    return _first_str(data.get("source"))


def build_result(item: dict, *, min_similarity: int = DEFAULT_MIN_SIMILARITY) -> SearchResult | None:
    """把单条 ``results[]`` 解析为 ``SearchResult``；``item`` 非 dict 时返回 ``None``。"""
    if not isinstance(item, dict):
        return None

    header = item.get("header")
    header = header if isinstance(header, dict) else {}
    data = item.get("data")
    data = data if isinstance(data, dict) else {}

    # similarity 是字符串 → 安全转 float；非法/缺失得 None（不崩）
    similarity = _to_float(header.get("similarity"), None)
    index_id = _coerce_int(header.get("index_id"))
    library = _library_name(index_id, header.get("index_name"))

    # 链接
    raw_ext = data.get("ext_urls")
    ext_urls = [u.strip() for u in raw_ext if isinstance(u, str) and u.strip()] if isinstance(raw_ext, list) else []
    pixiv_id = data.get("pixiv_id")
    member = data.get("member")
    url = _pick_primary_url(data, pixiv_id, ext_urls)

    # 画师信息：creator / author_name 优先；退而取 member（uid）
    artist = _first_str(data.get("creator"), data.get("author_name"))
    artist_url = _first_str(data.get("author_url"))
    member_str = _id_str(member)
    if not artist and member_str:
        artist = member_str
    if not artist_url and member_str:
        artist_url = f"https://www.pixiv.net/users/{member_str}"

    # 标题回退链（因库而异）：title → eng_name → jp_name
    title = _first_str(data.get("title"), data.get("eng_name"), data.get("jp_name"))

    thumbnail = header.get("thumbnail") if isinstance(header.get("thumbnail"), str) else None

    low_confidence = similarity is None or similarity < min_similarity

    extra = {
        "index_id": index_id,
        "index_name": library,
        "library": library,
        "pixiv_id": _id_str(pixiv_id) or None,
        "member": member_str or None,
        "artist": artist or None,
        "artist_url": artist_url or None,
        "ext_urls": ext_urls,
        "source": _first_str(data.get("source")) or None,
        "year": data.get("year"),
        "part": data.get("part"),
        "material": data.get("material"),
        "low_confidence": low_confidence,
        "tier": "main",
    }

    source = f"来自 {library} 库" if library else "SauceNAO"

    return SearchResult(
        title=title,
        source=source,
        url=url,
        thumbnail=thumbnail,
        score=similarity,
        extra=extra,
    )


def parse_saucenao_response(
    payload: Any,
    *,
    min_similarity: int = DEFAULT_MIN_SIMILARITY,
) -> SourceOutcome:
    """解析 SauceNAO ``output_type=2`` 的 JSON 响应为统一的 ``SourceOutcome``（纯函数）。

    容错：非 dict、``results`` 缺失/非 list、字段缺失均不抛异常。``header.status != 0``
    时在 ``meta`` 标记 ``status_error=True`` 并给出可读 ``warning``，由客户端据此**抛出
    可读 ``RuntimeError``**（不静默返回空）。
    """
    warnings: list[str] = []
    meta: dict = {
        "status": None,
        "status_error": False,
        "quota": {
            "short_remaining": None,
            "long_remaining": None,
            "short_limit": None,
            "long_limit": None,
        },
        "total_results": 0,
        "shown_results": 0,
    }

    if not isinstance(payload, dict):
        return SourceOutcome(results=[], warnings=["接口返回数据格式异常"], meta=meta)

    header = payload.get("header")
    header = header if isinstance(header, dict) else {}

    # ---- 状态码 ----
    status = _coerce_int(header.get("status"))
    meta["status"] = status
    if status is not None and status != 0:
        message = _status_message(status)
        meta["status_error"] = True
        meta["status_error_message"] = message
        warnings.append(message)
        return SourceOutcome(results=[], warnings=warnings, meta=meta)

    # ---- 配额（解析失败不报错）----
    quota = {
        "short_remaining": _coerce_int(header.get("short_remaining")),
        "long_remaining": _coerce_int(header.get("long_remaining")),
        "short_limit": _coerce_int(header.get("short_limit")),
        "long_limit": _coerce_int(header.get("long_limit")),
    }
    meta["quota"] = quota

    long_remaining = quota["long_remaining"]
    short_remaining = quota["short_remaining"]
    if long_remaining is not None and long_remaining <= 0:
        warnings.append("⚠️ SauceNAO 今日配额已用完（免费账户 150 次/天），请明天再试。")
    if short_remaining is not None and short_remaining <= 0:
        warnings.append("⚠️ SauceNAO 触发了限流（免费账户 4 次/30 秒），请等待约 30 秒后再试。")

    # ---- 结果 ----
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []

    collected: list[SearchResult] = []
    for item in raw_results:
        result = build_result(item, min_similarity=min_similarity)
        if result is not None:
            collected.append(result)

    # 按相似度降序（None 排最后），响应虽已排序，仍自行兜底
    collected.sort(
        key=lambda r: (r.score is not None, r.score if r.score is not None else 0.0),
        reverse=True,
    )

    # 低于 min_similarity 的过滤掉（分数未知者保留，交由上层判定）
    filtered = [r for r in collected if r.score is None or r.score >= min_similarity]

    meta["total_results"] = len(collected)
    meta["shown_results"] = len(filtered)

    if filtered and all(r.extra.get("low_confidence") for r in filtered):
        warnings.append(f"最佳相似度低于阈值（{min_similarity}%），结果仅供参考。")

    return SourceOutcome(results=filtered, warnings=warnings, meta=meta)


class SaucenaoClient:
    """SauceNAO 以图反查客户端（multipart 上传 + JSON 解析）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str = "",
        db_mask: int = DEFAULT_DB_MASK,
        numres: int = DEFAULT_NUMRES,
        min_similarity: int = DEFAULT_MIN_SIMILARITY,
        hide: int = DEFAULT_HIDE,
        timeout: int = 30,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = str(api_key).strip() if isinstance(api_key, str) else ""
        self.db_mask = resolve_db_mask(db_mask)
        self.numres = resolve_numres(numres)
        self.min_similarity = resolve_min_similarity(min_similarity)
        self.hide = resolve_hide(hide)
        self.timeout = max(5, int(timeout))
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """释放底层 aiohttp 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=float(self.timeout), connect=min(15, self.timeout))
                    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5, ttl_dns_cache=300)
                    self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    def _build_params(self) -> dict[str, str]:
        """构造查询参数（除图片外的全部字段）。

        ``dbmask`` 语义：位掩码 **0 表示"不启用任何库"**，属位掩码惯例。因此当掩码为 0
        （用户配置 ``saucenao_db_mask=0``，意图"不限库/搜全部"）时**不发送 ``dbmask`` 参数**——
        不传即由服务端按默认（全库）处理，避免误发 ``dbmask=0`` 导致"搜不到"。
        （该行为为离线推断，**待真机确认**，见 README 已知限制。）
        """
        params: dict[str, str] = {
            "output_type": "2",
            "numres": str(self.numres),
            "minsim": str(self.min_similarity),
            "hide": str(self.hide),
        }
        if self.db_mask > 0:
            params["dbmask"] = str(self.db_mask)
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    @staticmethod
    def build_form(image: bytes, *, filename: str = "query.jpg", mime: str = "image/jpeg") -> aiohttp.FormData:
        """构造 multipart 表单（字段名 ``file``）。抽成独立方法便于联网校验脚本离线自检字段名。"""
        form = aiohttp.FormData()
        form.add_field("file", image, filename=filename or "query.jpg", content_type=mime or "image/jpeg")
        return form

    async def search(
        self,
        image: bytes,
        *,
        filename: str = "query.jpg",
        mime: str = "image/jpeg",
    ) -> SourceOutcome:
        """上传图片执行反查。

        Args:
            image: 图片二进制。
            filename: 上传文件名（须带扩展名）。
            mime: 图片 MIME 类型。

        Returns:
            解析后的 ``SourceOutcome``。

        Raises:
            ValueError: 图片数据为空。
            RuntimeError: 网络层（超时/传输错误/非 200）、响应非 JSON，或 ``header.status != 0``。
        """
        if not image:
            raise ValueError("图片数据为空")

        # 局部导入避免模块级循环依赖（与 safebooru_client 一致）
        from .image_source import BROWSER_UA

        url = f"{self.base_url}/search.php"
        params = self._build_params()
        form = self.build_form(image, filename=filename, mime=mime)

        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json, text/javascript, */*;q=0.1",
            "Referer": f"{self.base_url}/",
        }

        session = await self._get_session()
        db_info = (
            "不限定库（不发送 dbmask 参数）"
            if self.db_mask <= 0
            else f"{self.db_mask}（{describe_db_mask(self.db_mask)}）"
        )
        logger.info(
            "[搜图] saucenao 请求: dbmask=%s numres=%s minsim=%s hide=%s key=%s bytes=%d",
            db_info,
            self.numres,
            self.min_similarity,
            self.hide,
            "有" if self.api_key else "无",
            len(image),
        )

        # 网络层：区分超时 / 传输错误 / HTTP 状态码，统一包装为可读 RuntimeError
        try:
            async with session.post(url, params=params, data=form, headers=headers) as resp:
                status = resp.status
                if status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"SauceNAO 接口失败 HTTP {status}: {body[:200]}")
                body = await resp.text()
        except asyncio.TimeoutError as exc:
            raise RuntimeError("SauceNAO 请求超时，请稍后重试（大陆通常需配置代理）") from exc
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"SauceNAO 网络错误（大陆通常需配置代理）: {exc}") from exc

        # 解析层：SauceNAO 正常必返回 JSON；非 JSON 视为异常（可能被拦截/接口变更）
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "SauceNAO 接口返回非 JSON（可能被拦截或接口已变更）"
            ) from exc

        outcome = parse_saucenao_response(payload, min_similarity=self.min_similarity)

        # 状态码非 0 → 抛出可读错误，绝不静默返回空结果
        if outcome.meta.get("status_error"):
            message = outcome.meta.get("status_error_message") or "SauceNAO 返回错误状态"
            raise RuntimeError(message)

        return outcome
