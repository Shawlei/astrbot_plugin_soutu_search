"""TTL 缓存。

用于缓存搜图结果，避免同一张图片 / 同一个关键词在短时间内重复请求外部站点，
既节省站点算力，也提升响应速度。

实现说明：
- AstrBot 插件运行在 asyncio 单线程事件循环中，``dict`` 的读写在单线程内本就不会
  被抢占，因此**不额外加锁**（实现与文档保持一致，不留死代码）。
- ``ttl <= 0`` 表示不缓存（等价于关闭缓存），便于通过配置项 ``cache_ttl=0`` 关闭。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any


def sha256_hex(data: bytes) -> str:
    """计算 bytes 的 sha256 十六进制摘要。"""
    return hashlib.sha256(data).hexdigest()


def make_image_key(data: bytes) -> str:
    """以图片内容的 sha256 作为缓存键（内容相同则命中同一缓存）。"""
    return "img:" + sha256_hex(data)


def make_saucenao_key(data: bytes) -> str:
    """SauceNAO 反查的缓存键（**独立命名空间**，避免与 soutubot 的图片缓存互相污染）。"""
    return "snao:" + sha256_hex(data)


def make_yandex_key(data: bytes) -> str:
    """Yandex 反查的缓存键（**独立命名空间**，避免与其他图源互相污染）。"""
    return "yadx:" + sha256_hex(data)


def make_ascii2d_key(data: bytes, *, bovw: bool = False) -> str:
    """ascii2d 反查的缓存键（**独立命名空间**，避免与 soutubot / SauceNAO 互相污染）。

    把 ``bovw`` 纳入键：特征检索模式与色彩检索模式的结果不同，不能互相命中。
    """
    return ("a2d-bovw:" if bovw else "a2d:") + sha256_hex(data)


def make_tags_key(tags: str, *, rating: str = "safe", limit: int = 3, page: int = 0) -> str:
    """以关键词 + 过滤参数构造缓存键。"""
    normalized = " ".join(str(tags).split()).strip().lower()
    return f"tags:{rating}:{int(limit)}:{int(page)}:{normalized}"


class TTLCache:
    """带过期时间的简单键值缓存。"""

    def __init__(self, default_ttl: int = 3600) -> None:
        self.default_ttl: float = float(max(0, int(default_ttl)))
        # key -> (expire_at_epoch, value)；expire_at <= 0 表示永不过期
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """取值；键不存在或已过期时返回 ``None``。"""
        item = self._store.get(key)
        if item is None:
            return None
        expire_at, value = item
        if expire_at > 0 and time.time() >= expire_at:
            # 惰性删除：读取到过期项时顺手清理
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """写入缓存；``ttl <= 0`` 时不写入（等效于关闭缓存）。"""
        effective_ttl = self.default_ttl if ttl is None else float(ttl)
        if effective_ttl <= 0:
            # 关闭缓存：不做任何存储
            return
        self._store[key] = (time.time() + effective_ttl, value)

    def delete(self, key: str) -> None:
        """删除指定键。"""
        self._store.pop(key, None)

    def clear(self) -> None:
        """清空全部缓存。"""
        self._store.clear()

    def prune(self) -> int:
        """清理所有已过期键，返回清理数量。"""
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if exp > 0 and now >= exp]
        for key in expired:
            self._store.pop(key, None)
        return len(expired)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None
