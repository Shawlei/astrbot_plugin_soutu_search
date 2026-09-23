"""SSRF 地址段回归测试：CGNAT 及"非全局单播"边角网段（P2 修复护栏）。

背景：``_is_blocked_ip`` 原判定（内网/环回/链路本地/保留/组播/未指定）**漏掉了**
CGNAT / 共享地址段 ``100.64.0.0/10``（RFC 6598）—— 它的 ``is_private`` /
``is_reserved`` / ``is_global`` **全为 False**，因而被放行。本轮新增的"图片直链"入口
（`搜本/搜P站 <图片链接>`）使该段**首次可由用户纯文本触达**，故补上 ``not is_global``。

本文件同时**自证不误伤**真实公网图床（必须放行），并覆盖 CGNAT 网段**两端边界**。

运行::
    python -m unittest tests.test_ssrf_ranges -v
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.image_source import (  # noqa: E402
    ImageSource,
    _is_blocked_ip,
)


def _ip(s):
    return ipaddress.ip_address(s)


# 必须拦截：原有内网/特殊段 + 本轮新增的 CGNAT（唯一"三判定全 False"缺口段）
MUST_BLOCK = [
    # 原有
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.10",
    "169.254.169.254", "0.0.0.0", "::1",
    # CGNAT / 共享地址 100.64.0.0/10（两端 + 中间）
    "100.64.0.0", "100.64.0.1", "100.64.128.1", "100.127.255.255",
]

# 必须放行：真实公网（裸 IP 字面量，确定性，无 DNS）
MUST_ALLOW = [
    "1.1.1.1", "1.2.3.4", "8.8.8.8", "203.205.0.1", "114.114.114.114",
    # 上面 6 个域名在本机解析出的代表公网 IP
    "172.64.152.95", "104.21.34.109", "36.155.187.140", "120.232.23.140",
    "199.96.58.177",
]

# 真实图床/公网域名（放行性依赖本机 DNS，解析失败则 skip）
PUBLIC_DOMAINS = [
    "soutubot.moe",
    "safebooru.org",
    "gchat.qpic.cn",
    "c2cpicdw.qpic.cn",
    "multimedia.nt.qq.com.cn",
    "i.pximg.net",
]


class TestBlockedRanges(unittest.TestCase):
    def test_must_block(self):
        for s in MUST_BLOCK:
            with self.subTest(ip=s):
                self.assertTrue(_is_blocked_ip(_ip(s)), f"{s} 应被拦截")

    def test_must_allow(self):
        for s in MUST_ALLOW:
            with self.subTest(ip=s):
                self.assertFalse(_is_blocked_ip(_ip(s)), f"{s} 应被放行（不得误伤公网）")

    def test_cgnat_boundaries(self):
        """CGNAT 网段 100.64.0.0/10：两端必须拦截，紧邻的网段外地址必须放行。"""
        # 网段内（含两端）
        for s in ("100.64.0.0", "100.64.0.1", "100.127.255.255"):
            with self.subTest(inside=s):
                self.assertTrue(_is_blocked_ip(_ip(s)), f"{s} 属 CGNAT，应拦截")
        # 网段外紧邻（100.63.x 与 100.128.x 是公网，应放行）
        for s in ("100.63.255.255", "100.128.0.0"):
            with self.subTest(outside=s):
                self.assertFalse(_is_blocked_ip(_ip(s)), f"{s} 在 /10 之外，应放行")

    def test_ipv4_mapped_cgnat_blocked(self):
        """IPv4-mapped IPv6 形态的 CGNAT 也必须被拦截。"""
        for s in ("::ffff:100.64.0.1", "::ffff:100.127.255.255"):
            with self.subTest(ip=s):
                self.assertTrue(_is_blocked_ip(_ip(s)), f"{s} 应被拦截")

    def test_ipv4_mapped_loopback_still_blocked(self):
        self.assertTrue(_is_blocked_ip(_ip("::ffff:127.0.0.1")))

    def test_ipv6_local_still_blocked(self):
        for s in ("::", "::1", "fe80::1", "fc00::1", "fd00::1"):
            with self.subTest(ip=s):
                self.assertTrue(_is_blocked_ip(_ip(s)), f"{s} 应被拦截")


class TestHostBlockedLiterals(unittest.TestCase):
    """`_host_blocked` 对 IP 字面量不做 DNS（确定性），可直接断言。"""

    def test_cgnat_literal_blocked(self):
        for s in ("100.64.0.1", "100.64.0.0", "100.127.255.255"):
            with self.subTest(host=s):
                self.assertTrue(ImageSource._host_blocked(s), f"{s} 应被拒绝")

    def test_public_literal_allowed(self):
        for s in ("1.2.3.4", "8.8.8.8", "1.1.1.1"):
            with self.subTest(host=s):
                self.assertFalse(ImageSource._host_blocked(s), f"{s} 应放行")

    def test_obfuscated_still_blocked(self):
        for s in ("2130706433", "0x7f000001", "0177.0.0.1", "127.1",
                  "100.64.0.1", "::ffff:127.0.0.1", "[::ffff:127.0.0.1]"):
            with self.subTest(host=s):
                self.assertTrue(ImageSource._host_blocked(s), f"{s} 应被拒绝")


class TestPublicDomainsNotOverBlocked(unittest.TestCase):
    """自证不误伤：6 个真实图床域名（依赖本机 DNS，解析失败则 skip 而非误判）。"""

    def test_domains_all_resolved_ips_allowed(self):
        for domain in PUBLIC_DOMAINS:
            with self.subTest(domain=domain):
                try:
                    infos = socket.getaddrinfo(domain, 443)
                except Exception:  # noqa: BLE001 - 无网络/DNS 时跳过，不误判
                    self.skipTest(f"{domain} 无法解析（本机无 DNS/网络），跳过")
                ips = sorted({info[4][0] for info in infos})
                self.assertTrue(ips, f"{domain} 应有解析结果")
                for raw in ips:
                    self.assertFalse(
                        _is_blocked_ip(ipaddress.ip_address(raw)),
                        f"{domain} 解析出的 {raw} 被误拦",
                    )
                # 域名整体判定也必须放行
                self.assertFalse(ImageSource._host_blocked(domain), f"{domain} 被误拦")


class TestOtherSpecialRangesNotGaps(unittest.TestCase):
    """排查结论（item 4）：除 CGNAT 外，**没有**其它"三判定全 False"的缺口段。

    AS112（`192.31.196.0/24`、`192.175.48.0/24`）、AMT（`192.52.193.0/24`）、
    已弃用 6to4 中继（`192.88.99.0/24`）、PCP/TURN anycast（`192.0.0.9`、`192.0.0.10`）
    在 Python 的 `ipaddress` 中 `is_global` **为 True**（属"全球可路由"的 anycast 用途段），
    并不满足"`is_private`/`is_reserved`/`is_global` 三个全 False"，因此**不属本轮修复范畴**。
    此处显式断言，以便未来变动是"有意为之"而非悄悄漂移。
    """

    def test_those_anycast_blocks_are_global(self):
        for s in ("192.31.196.1", "192.52.193.1", "192.175.48.1",
                  "192.88.99.1", "192.0.0.9", "192.0.0.10"):
            with self.subTest(ip=s):
                self.assertTrue(ipaddress.ip_address(s).is_global,
                                f"{s} 应为全局可路由（故不属非全局缺口）")

    def test_cgnat_is_the_only_non_global_gap(self):
        # CGNAT：三个判定全 False，故必须由 `not is_global` 兜住
        ip = ipaddress.ip_address("100.64.0.1")
        self.assertFalse(ip.is_private)
        self.assertFalse(ip.is_reserved)
        self.assertFalse(ip.is_global)
        self.assertTrue(_is_blocked_ip(ip))


if __name__ == "__main__":
    unittest.main(verbosity=2)
