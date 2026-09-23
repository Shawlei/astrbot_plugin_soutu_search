"""图片获取与规范化。

把 AstrBot 消息中的图片统一转换成 ``bytes`` 二进制，供上传搜图使用。

``Image`` 组件的 ``file`` 字段可能是三种形态之一：
1. ``http(s)://`` URL（QQ 的 ``c2cpicdw.qpic.cn`` / ``gchat.qpic.cn`` 等，**有防盗链**）
2. 本地文件路径 / ``file://`` URI
3. ``data:`` base64 data URI

另外还需处理**引用回复**（``Reply`` 组件）中携带的图片，以及「文本 + 图片」混合消息。
所有异常都在上层捕获并降级为友好提示，本模块自身只负责尽力取图。
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiofiles
import aiohttp

from astrbot.api import logger

# 尝试导入真实的 Image 组件类；失败时回退为鸭子类型判断（提升健壮性）
try:  # pragma: no cover - 取决于运行环境
    from astrbot.api.message_components import Image as _AstrImage
except Exception:  # pragma: no cover
    _AstrImage = None

# 正常浏览器 UA：下载图片与访问图库接口时都需要，避免被简单反爬拦截
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[^;,]*)?(?:;charset=[^;,]*)?(?P<b64>;base64)?,(?P<data>.*)$",
    re.IGNORECASE | re.DOTALL,
)

# MIME -> 扩展名
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/x-icon": "ico",
    "image/avif": "avif",
}

# 默认图片大小上限：10MB
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 允许的 URL 协议（其余如 file:/ftp:/gopher: 一律拒绝）
_ALLOWED_URL_SCHEMES = ("http", "https")

# 形如 scheme:// 的前缀
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://")


def _is_blocked_ip(ip) -> bool:
    """判断一个 ``ipaddress`` 对象是否属于内网 / 环回 / 链路本地 / 保留 / 组播 / 未指定地址。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）按其映射的 IPv4 判定
        return _is_blocked_ip(ip.ipv4_mapped)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _parse_ip_literal(host: str):
    """把 host 解析为 IP 对象；支持标准与「混淆形式」IPv4，非 IP 返回 None。

    混淆形式涵盖：十进制整数（``2130706433``）、十六进制（``0x7f000001``）、
    八进制（``0177.0.0.1``）、以及 ``inet_aton`` 的短写（``127.1``）。
    这些在 glibc 系（Linux/macOS）的 getaddrinfo 会被当作 IP 解析，必须自行兜住。
    """
    text = (host or "").strip()
    if not text:
        return None
    # 去 IPv6 方括号
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    # 标准 IP（含标准 IPv6）
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    # 混淆 IPv4
    return _parse_ipv4_obfuscated(text)


def _parse_ipv4_obfuscated(host: str):
    """解析 inet_aton 风格的 IPv4（十进制/十六进制/八进制/短写）；失败返回 None。"""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if part == "":
            return None
        try:
            if part.lower().startswith("0x"):
                numbers.append(int(part, 16))
            elif len(part) > 1 and part[0] == "0":
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None
    if len(numbers) == 1:
        if numbers[0] > 0xFFFFFFFF:
            return None
        value = numbers[0]
    elif len(numbers) == 2:
        if numbers[0] > 0xFF or numbers[1] > 0xFFFFFF:
            return None
        value = (numbers[0] << 24) | numbers[1]
    elif len(numbers) == 3:
        if numbers[0] > 0xFF or numbers[1] > 0xFF or numbers[2] > 0xFFFF:
            return None
        value = (numbers[0] << 24) | (numbers[1] << 16) | numbers[2]
    else:
        if any(number > 0xFF for number in numbers):
            return None
        value = (numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8) | numbers[3]
    try:
        return ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return None


def _resolve_host_ips(host: str) -> list[str] | None:
    """同步解析 host 得到 IP 列表；解析失败返回 ``None``（调用方需 fail-closed）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001 - 任何解析失败都视为失败
        return None
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else None
        if sockaddr:
            ips.append(str(sockaddr[0]))
    return ips


@dataclass
class ImagePayload:
    """规范化的待搜索图片。"""

    data: bytes
    mime: str = "image/jpeg"
    filename: str = "query.jpg"
    source_kind: str = "unknown"  # url | file | data | unknown


def is_http_url(value: str) -> bool:
    """是否为 http(s) 链接。"""
    return bool(value) and bool(_HTTP_RE.match(str(value).strip()))


def is_data_uri(value: str) -> bool:
    """是否为 data URI。"""
    return bool(value) and str(value).strip().lower().startswith("data:")


def classify_source(value: str) -> str:
    """判断来源类型：``url`` / ``data`` / ``file`` / ``unknown``。"""
    if not value or not str(value).strip():
        return "unknown"
    text = str(value).strip()
    if is_data_uri(text):
        return "data"
    if is_http_url(text):
        return "url"
    return "file"


def guess_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    """根据文件头猜测 MIME 类型。"""
    if not data:
        return fallback
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    return fallback


def guess_ext(mime: str, fallback: str = "jpg") -> str:
    """MIME -> 扩展名。"""
    if not mime:
        return fallback
    return _MIME_EXT.get(str(mime).strip().lower().split(";")[0].strip(), fallback)


def detect_image_mime(data: bytes) -> str | None:
    """按文件魔数判定真实图片类型；非图片返回 ``None``。

    支持的魔数：JPEG(FFD8FF)、PNG(89504E47)、GIF(474946)、WEBP(RIFF....WEBP)、BMP(424D)。
    """
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def strip_file_uri(uri: str) -> str:
    """把 ``file://`` URI 转成本地文件路径（兼容 Windows 盘符）。"""
    parsed = urlparse(str(uri))
    path = unquote(parsed.path)
    # file:///C:/a.jpg -> /C:/a.jpg -> C:/a.jpg
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    # file://server/share/a.jpg -> //server/share/a.jpg
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        path = f"//{parsed.netloc}{path}"
    return path


def parse_data_uri(uri: str) -> tuple[bytes, str]:
    """解析 data URI，返回 ``(bytes, mime)``。"""
    match = _DATA_URI_RE.match(str(uri).strip())
    if not match:
        raise ValueError("非法的 data URI")
    mime = (match.group("mime") or "image/jpeg").strip() or "image/jpeg"
    raw = match.group("data") or ""
    if match.group("b64"):
        data = base64.b64decode(raw, validate=False)
    else:
        data = unquote(raw).encode("utf-8", "ignore")
    if not data:
        raise ValueError("data URI 内容为空")
    return data, mime


def _filename_from_url(url: str) -> str:
    """从 URL 路径中提取文件名（无则返回空串）。"""
    try:
        name = os.path.basename(unquote(urlparse(str(url)).path))
        return name if name and "." in name else ""
    except Exception:
        return ""


def is_image_component(comp) -> bool:
    """判断组件是否为图片组件（优先 isinstance，回退鸭子类型）。"""
    if comp is None:
        return False
    if _AstrImage is not None and isinstance(comp, _AstrImage):
        return True
    return type(comp).__name__.lower() == "image"


def _reply_children(comp):
    """取出引用回复/嵌套组件的子组件列表；无则返回 None。"""
    if comp is None:
        return None
    for attr in ("chain", "content", "message"):
        value = getattr(comp, attr, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    return None


def _component_source(comp) -> str | None:
    """从图片组件中取出可用的来源字符串。"""
    for attr in ("file", "url", "path", "_file"):
        value = getattr(comp, attr, None)
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _flatten_components(items):
    """把消息组件（含引用回复的嵌套 chain）展平为叶子组件。"""
    if items is None:
        return
    if not isinstance(items, (list, tuple)):
        items = [items]
    for comp in items:
        if comp is None:
            continue
        if is_image_component(comp):
            yield comp
            continue
        children = _reply_children(comp)
        if children:
            yield from _flatten_components(children)


class ImageSource:
    """图片获取与规范化器（含 aiohttp 会话管理 + 安全校验）。"""

    def __init__(
        self,
        timeout: int = 30,
        *,
        allowed_roots=None,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        self.timeout = max(5, int(timeout))
        self.max_image_bytes = max(1, int(max_image_bytes))
        # 本地文件读取白名单根目录；为空表示**拒绝一切本地文件**（安全默认）
        self.allowed_roots = self._normalize_roots(allowed_roots)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize_roots(allowed_roots) -> list[Path]:
        roots: list[Path] = []
        for item in allowed_roots or []:
            try:
                roots.append(Path(item).resolve())
            except Exception:  # noqa: BLE001
                roots.append(Path(item).absolute())
        return roots

    async def close(self) -> None:
        """释放底层 aiohttp 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(
                        total=float(self.timeout),
                        connect=min(15, self.timeout),
                    )
                    connector = aiohttp.TCPConnector(limit=10, limit_per_host=5, ttl_dns_cache=300)
                    self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    # ---------------------------- 安全校验 ---------------------------- #
    def _validate_image(self, data: bytes) -> str:
        """校验大小与魔数，返回以魔数推断的 MIME；不合法则抛 ``RuntimeError``。"""
        if not data:
            raise RuntimeError("图片内容为空")
        if len(data) > self.max_image_bytes:
            raise RuntimeError(
                f"图片过大（{len(data)} 字节，超过上限 {self.max_image_bytes} 字节）"
            )
        mime = detect_image_mime(data)
        if mime is None:
            raise RuntimeError("无法识别的图片格式（魔数校验失败，非 JPEG/PNG/GIF/WEBP/BMP）")
        return mime

    @staticmethod
    def _host_blocked(host: str | None) -> bool:
        """判断主机是否应被拒绝。**fail-closed**：解析失败也视为拒绝。

        拦截策略（纵深防御）：
        1. 空 host → 拒绝；``localhost`` / ``*.localhost`` → 拒绝。
        2. **字面量 IP**（含十进制/十六进制/八进制/短写等混淆形式）→ 直接按 IP 段判定。
        3. **域名** → 同步 DNS 解析（``socket.getaddrinfo``），**逐一**校验解析出的所有 IP，
           任一落在内网/环回/链路本地/保留/组播/未指定地址段内即拒绝；
           **解析失败（无结果/异常）同样拒绝**（覆盖 ``2130706433`` 等在部分平台解析失败的形态）。

        .. note::
           本方法为**同步**实现（会做阻塞式 DNS），仅供探针/单元测试直接调用；
           在异步路径中请使用 :meth:`_host_is_blocked`（在线程池中执行，不阻塞事件循环）。
        """
        if not host:
            return True
        literal = host.strip()
        if literal.startswith("[") and literal.endswith("]"):
            literal = literal[1:-1]
        literal = literal.strip().lower()
        if not literal:
            return True
        if literal == "localhost" or literal.endswith(".localhost"):
            return True

        # 第一道防线：字面量 / 混淆 IP（无需 DNS，直接判定）
        ip = _parse_ip_literal(literal)
        if ip is not None:
            return _is_blocked_ip(ip)

        # 第二道防线：DNS 解析后逐一校验（fail-closed）
        ips = _resolve_host_ips(literal)
        if ips is None:
            return True  # 解析失败 → 拒绝
        if not ips:
            return True
        for candidate in ips:
            parsed_ip = _parse_ip_literal(candidate)
            if parsed_ip is None or _is_blocked_ip(parsed_ip):
                return True
        return False

    async def _host_is_blocked(self, host: str | None) -> bool:
        """异步版 ``_host_blocked``：把阻塞式 DNS 放到线程池，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._host_blocked, host)

    def _within_allowed(self, path: Path) -> bool:
        """判断路径是否位于允许的根目录之内（含子目录）。"""
        if not self.allowed_roots:
            return False
        try:
            resolved = path.resolve()
        except Exception:  # noqa: BLE001
            resolved = path.absolute()
        for root in self.allowed_roots:
            try:
                if os.path.commonpath([str(resolved), str(root)]) == str(root):
                    return True
            except ValueError:
                continue  # 不同盘符，必然不在该根目录内
        return False

    async def fetch_url(self, url: str, referer: str | None = None, max_redirects: int = 5) -> bytes:
        """下载 URL 图片，返回 bytes。

        - **每一跳都校验**：手动处理重定向（禁用自动跳转），对每个 Location 重新做
          协议白名单 + 解析后 IP 校验，杜绝「先跳到公网再重定向到内网」的绕过。
        - 针对 QQ 图片的防盗链，每跳做两级尝试：先直接请求；若失败再补常见 Referer 重试。
        """
        session = await self._get_session()
        base_headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if referer:
            base_headers["Referer"] = referer

        attempts: list[dict[str, str]] = [dict(base_headers)]
        if not referer:
            retry_headers = dict(base_headers)
            retry_headers["Referer"] = "https://qq.com/"
            attempts.append(retry_headers)

        current = url
        for _hop in range(max_redirects + 1):
            parsed = urlparse(current)
            if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
                raise RuntimeError(f"重定向到不允许的协议: {parsed.scheme or current}")
            if await self._host_is_blocked(parsed.hostname):
                raise RuntimeError(
                    f"出于安全考虑，拒绝访问内网/保留地址: {parsed.hostname or current}"
                )

            last_error: Exception | None = None
            redirect_location: str | None = None
            saw_redirect = False
            for headers in attempts:
                try:
                    async with session.get(current, headers=headers, allow_redirects=False) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            saw_redirect = True
                            redirect_location = resp.headers.get("Location") if resp.headers else None
                            break
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
                                return data
                            last_error = RuntimeError("响应为空")
                        else:
                            last_error = RuntimeError(f"HTTP {resp.status}")
                except aiohttp.ClientError as exc:
                    last_error = exc

            if saw_redirect:
                if not redirect_location:
                    raise RuntimeError("重定向缺少 Location 头，已中止")
                current = urljoin(current, redirect_location)
                continue

            raise RuntimeError(f"图片下载失败: {last_error}")

        raise RuntimeError("重定向次数过多，已中止")

    async def from_source(self, source: str) -> ImagePayload:
        """把任意来源字符串规范化为 ``ImagePayload``（含安全校验）。"""
        text = str(source).strip() if source is not None else ""
        if not text:
            raise ValueError("图片来源为空")

        # 1) data URI
        if is_data_uri(text):
            data, _declared = parse_data_uri(text)
            mime = self._validate_image(data)
            return ImagePayload(
                data=data,
                mime=mime,
                filename=f"query.{guess_ext(mime)}",
                source_kind="data",
            )

        scheme_match = _SCHEME_RE.match(text)
        scheme = scheme_match.group(1).lower() if scheme_match else ""

        # 2) http(s) URL —— 仅允许 http/https，且拒绝内网/保留地址（含 DNS 解析，fail-closed）
        if scheme in _ALLOWED_URL_SCHEMES:
            parsed = urlparse(text)
            if await self._host_is_blocked(parsed.hostname):
                raise RuntimeError(
                    f"出于安全考虑，拒绝访问内网/保留地址: {parsed.hostname or text}"
                )
            data = await self.fetch_url(text)
            mime = self._validate_image(data)
            filename = _filename_from_url(text) or f"query.{guess_ext(mime)}"
            return ImagePayload(data=data, mime=mime, filename=filename, source_kind="url")

        # 3) 其它显式协议（file:/ftp:/gopher: 等）
        if scheme and scheme != "file":
            raise ValueError(f"不支持的图片来源协议: {scheme}（仅允许 http/https/file/data）")

        # 4) 本地路径 / file:// URI —— 必须位于允许的根目录之内
        path_str = strip_file_uri(text) if (scheme == "file" or text.lower().startswith("file:")) else text
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError(f"本地图片不存在: {path_str}")
        if not self._within_allowed(path):
            raise PermissionError(f"本地图片路径不在允许目录内，已拒绝: {path_str}")
        async with aiofiles.open(path, "rb") as handle:
            data = await handle.read()
        mime = self._validate_image(data)
        return ImagePayload(
            data=data,
            mime=mime,
            filename=path.name or f"query.{guess_ext(mime)}",
            source_kind="file",
        )

    async def from_component(self, comp) -> ImagePayload | None:
        """从单个组件（或其引用回复子组件）中取图。"""
        if not is_image_component(comp):
            children = _reply_children(comp)
            if children:
                for child in children:
                    payload = await self.from_component(child)
                    if payload is not None:
                        return payload
            return None

        source = _component_source(comp)
        if not source:
            return None
        return await self.from_source(source)

    def _event_components(self, event) -> list:
        """取出事件消息中的全部组件（兼容不同字段名）。"""
        raw = None
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            raw = getattr(message_obj, "message", None)
        if raw is None:
            raw = getattr(event, "message", None)
        return list(_flatten_components(raw))

    async def from_event(self, event) -> ImagePayload | None:
        """从消息事件中提取第一张可用图片，取不到返回 ``None``。"""
        for comp in self._event_components(event):
            try:
                payload = await self.from_component(comp)
            except Exception as exc:
                logger.warning("[搜图] 解析图片组件失败: %s", exc)
                payload = None
            if payload is not None:
                return payload
        return None
