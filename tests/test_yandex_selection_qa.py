"""QA 对抗/边界测试：Yandex「结果选样策略」修复的独立验证（Edward / 严过关）。

与 ``tests/test_yandex.py``（工程师用例）**互补**，聚焦工程师可能未覆盖的对抗面：
- 域名后缀匹配「不过度匹配」（伪装域名不得被判 high）；
- Pinterest 品牌标签匹配「不误伤」相似域名；
- 大小写 / 协议 / 端口 / userinfo 归一化健壮性；
- 空输入 / 残结构：绝不抛异常；
- 去重边界（max_per_domain=1/0/负数）；
- top_k 边界（1 / 超总数 / 0 / 负数）；
- 兜底不空 + 诚实提示；
- formatter additive 改动对其它 provider 逐字不变（回归）；
- warnings 双 emoji 检查。

运行：
  cd astrbot_plugin_soutu_search
  python -m unittest tests.test_yandex_selection_qa -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PLUGIN_ROOT.parent), str(PLUGIN_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tests.test_core as _stub  # noqa: E402,F401  (装配 astrbot 桩)

from astrbot_plugin_soutu_search.core.formatter import (  # noqa: E402
    SearchResult,
    SourceOutcome,
    _result_line,
    format_outcome,
)
from astrbot_plugin_soutu_search.core.yandex_client import (  # noqa: E402
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_NEUTRAL,
    SIMILAR_ONLY_WARNING,
    _matches_suffix,
    build_yandex_warnings,
    classify_source,
    parse_yandex_html,
)


def _html(sites=None, similar=None, init_override=None) -> str:
    """构造带 data-state（≥300 字符）的最小结果页。"""
    init: dict = {}
    if sites is not None:
        init["cbirSites"] = {"sites": sites}
    if similar is not None:
        init["cbirSimilar"] = {"thumbs": similar}
    if init_override is not None:
        init = dict(init_override)
    pad = 400 - len(json.dumps({"initialState": init}, ensure_ascii=False))
    if pad > 0:
        init["pad"] = "x" * pad
    state = json.dumps({"initialState": init}, ensure_ascii=False).replace('"', "&quot;")
    return f'<html><body><div data-state="{state}"></div></body></html>'


def _site(index: int, domain: str = "example.com", url: str | None = None, title: str | None = None) -> dict:
    return {
        "title": title if title is not None else f"t{index}",
        "description": "d",
        "url": url or f"https://host{index}.example/{index}",
        "domain": domain,
        "thumb": {"url": "//avatars.mds.yandex.net/i?id=x"},
    }


class TestSuffixOvermatchAdversarial(unittest.TestCase):
    """域名后缀匹配绝不能「过度匹配」：伪装域名不得被判为高价值。"""

    def test_exact_suffix_not_substring(self):
        # notdanbooru.donmai.us 不应命中 danbooru.donmai.us（后面不是 .danbooru.donmai.us）
        self.assertFalse(_matches_suffix("notdanbooru.donmai.us", "danbooru.donmai.us"))
        # 但它是 donmai.us 的合法子域 → 命中 donmai.us（这是**刻意**设计：覆盖 hijiribe 等子站）
        self.assertTrue(_matches_suffix("notdanbooru.donmai.us", "donmai.us"))
        # 结论：整体判 high，但**归因于 donmai.us 而非 danbooru.donmai.us**（设计如此，非误判 danbooru）
        self.assertEqual(classify_source("notdanbooru.donmai.us"), LEVEL_HIGH)

    def test_evil_suffix_never_high(self):
        # 高价值后缀**作为前缀**出现的伪装域名，一律不得判 high
        for evil in (
            "donmai.us.evil.com",
            "danbooru.donmai.us.evil.com",
            "x.com.evil.org",
            "pixiv.net.evil.com",
            "safebooru.org.evil.net",
            "twitter.com.evil.io",
        ):
            self.assertNotEqual(classify_source(evil), LEVEL_HIGH, evil)

    def test_evil_suffix_specific_expected(self):
        # 明确期望：这些伪装域名应为 neutral（既不是权威图库，也非已知搬运站）
        self.assertEqual(classify_source("donmai.us.evil.com"), LEVEL_NEUTRAL)
        self.assertEqual(classify_source("x.com.evil.org"), LEVEL_NEUTRAL)
        self.assertEqual(classify_source("danbooru.donmai.us.evil.com"), LEVEL_NEUTRAL)

    def test_legit_subdomain_still_high(self):
        # 正向对照：真子域仍应判 high
        self.assertEqual(classify_source("danbooru.donmai.us"), LEVEL_HIGH)
        self.assertEqual(classify_source("safebooru.donmai.us"), LEVEL_HIGH)
        self.assertEqual(classify_source("hijiribe.donmai.us"), LEVEL_HIGH)


class TestBrandFalsePositive(unittest.TestCase):
    """Pinterest 品牌标签匹配不得误伤含相似字符串的域名。"""

    def test_not_pinterest(self):
        self.assertEqual(classify_source("notpinterest.com"), LEVEL_NEUTRAL)

    def test_pinterest_clone_hyphen(self):
        # 含连字符：split(".") 得到 ["pinterest-clone","org"]，标签不等于 pinterest → neutral
        self.assertEqual(classify_source("pinterest-clone.org"), LEVEL_NEUTRAL)

    def test_pinterest_like_prefix(self):
        self.assertEqual(classify_source("mypinterest.com"), LEVEL_NEUTRAL)
        self.assertEqual(classify_source("pinterestx.com"), LEVEL_NEUTRAL)

    def test_real_pinterest_variants_low(self):
        for host in (
            "pinterest.com",
            "pinterest.ru",
            "pinterest.co.uk",
            "za.pinterest.com",
            "ru.pinterest.com",
            "www.pinterest.com",
        ):
            self.assertEqual(classify_source(host), LEVEL_LOW, host)


class TestCaseAndNormalization(unittest.TestCase):
    """大小写 / 协议 / 端口 / userinfo 归一化健壮性。"""

    def test_uppercase_domain_still_high(self):
        self.assertEqual(classify_source("DANBOORU.DONMAI.US"), LEVEL_HIGH)
        self.assertEqual(classify_source("WWW.PIXIV.NET"), LEVEL_HIGH)

    def test_uppercase_pinterest_still_low(self):
        self.assertEqual(classify_source("RU.PINTEREST.COM"), LEVEL_LOW)

    def test_url_with_scheme_port_userinfo(self):
        self.assertEqual(classify_source("https://User@www.pixiv.net:443/x"), LEVEL_HIGH)
        self.assertEqual(classify_source("http://www.pinterest.com/pin/1"), LEVEL_LOW)

    def test_trailing_dot_and_whitespace(self):
        self.assertEqual(classify_source("  pixiv.net.  "), LEVEL_HIGH)


class TestEmptyAndMalformedInputs(unittest.TestCase):
    """空 / 残结构输入绝不抛异常，一律优雅降级为空列表。"""

    def test_falsy_inputs_never_raise(self):
        for arg in ("", "<html></html>", None, b"<html>", 123, [], {}):
            with self.subTest(arg=repr(arg)):
                try:
                    out = parse_yandex_html(arg)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"parse_yandex_html({arg!r}) 抛异常: {exc!r}")
                self.assertEqual(out, [])

    def test_no_initial_state(self):
        state = json.dumps({"other": 1}).ljust(400, "x")
        self.assertEqual(parse_yandex_html(f'<div data-state="{state}"></div>'), [])

    def test_initial_state_non_dict(self):
        state = json.dumps({"initialState": "notdict"}).ljust(400, "x")
        self.assertEqual(parse_yandex_html(f'<div data-state="{state}"></div>'), [])

    def test_sites_non_list(self):
        self.assertEqual(parse_yandex_html(_html(init_override={"cbirSites": {"sites": "nope"}})), [])

    def test_cbir_sites_non_dict(self):
        self.assertEqual(parse_yandex_html(_html(init_override={"cbirSites": []})), [])

    def test_malformed_items_skipped(self):
        sites = [
            "garbage",
            None,
            123,
            {"url": None, "domain": "danbooru.donmai.us"},     # url None → 跳过
            {"url": 123, "domain": "pixiv.net"},               # url 非 str → 跳过
            {"domain": "example.com"},                          # 无 url → 跳过
            _site(1, "safebooru.org", "https://safebooru.org/post/1"),  # 唯一合法
        ]
        results = parse_yandex_html(_html(sites=sites))
        self.assertEqual([r.source for r in results], ["safebooru.org"])

    def test_similar_fallback_malformed_items(self):
        similar = ["bad", None, {"title": "ok", "linkUrl": "/x"}]
        results = parse_yandex_html(_html(sites=[], similar=similar))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "yandex-similar")


class TestDedupBoundaries(unittest.TestCase):
    """域名去重边界：max_per_domain 的 1 / 0 / 负数行为。"""

    def _same_domain_sites(self, n=5):
        return [
            _site(i, "danbooru.donmai.us", f"https://danbooru.donmai.us/posts/{i}")
            for i in range(n)
        ]

    def test_max_per_domain_one(self):
        results = parse_yandex_html(_html(sites=self._same_domain_sites()), top_k=10, max_per_domain=1)
        self.assertEqual(len(results), 1)

    def test_max_per_domain_two(self):
        results = parse_yandex_html(_html(sites=self._same_domain_sites()), top_k=10, max_per_domain=2)
        self.assertEqual(len(results), 2)

    def test_max_per_domain_zero_or_negative_falls_back_to_default(self):
        # 实际行为：0 / 负数被 _coerce_max_per_domain 归一化为默认值 2（不会清空结果，有最小保底）
        for bad in (0, -1, -99):
            with self.subTest(max_per_domain=bad):
                results = parse_yandex_html(_html(sites=self._same_domain_sites()), top_k=10, max_per_domain=bad)
                self.assertEqual(len(results), 2)

    def test_max_per_domain_invalid_type_falls_back(self):
        results = parse_yandex_html(_html(sites=self._same_domain_sites()), top_k=10, max_per_domain="abc")  # type: ignore[arg-type]
        self.assertEqual(len(results), 2)

    def test_pinterest_family_collapsed(self):
        domains = ["pinterest.com", "ru.pinterest.com", "in.pinterest.com", "za.pinterest.com"]
        sites = [_site(i, d, f"https://h{i}.x/{i}") for i, d in enumerate(domains)]
        results = parse_yandex_html(_html(sites=sites), top_k=10, max_per_domain=2)
        self.assertEqual(len(results), 2)


class TestTopKBoundaries(unittest.TestCase):
    """top_k 边界：1 / 超总数 / 0 / 负数。"""

    def _distinct(self, n=5):
        return [_site(i, f"e{i}.com", f"https://e{i}.com/{i}") for i in range(n)]

    def test_top_k_one(self):
        self.assertEqual(len(parse_yandex_html(_html(sites=self._distinct()), top_k=1)), 1)

    def test_top_k_greater_than_total(self):
        self.assertEqual(len(parse_yandex_html(_html(sites=self._distinct(5)), top_k=99)), 5)

    def test_top_k_zero_coerced_to_one(self):
        # 实际行为：0 → 至少给 1 条（_coerce_top_k 保证 ≥1）
        self.assertEqual(len(parse_yandex_html(_html(sites=self._distinct()), top_k=0)), 1)

    def test_top_k_negative_coerced_to_one(self):
        self.assertEqual(len(parse_yandex_html(_html(sites=self._distinct()), top_k=-5)), 1)

    def test_top_k_invalid_type(self):
        self.assertEqual(len(parse_yandex_html(_html(sites=self._distinct()), top_k="x")), 1)  # type: ignore[arg-type]


class TestFallbackNonEmpty(unittest.TestCase):
    """兜底不空：全低价值来源时仍返回结果并给出诚实提示。"""

    def test_all_low_not_empty_and_warns(self):
        low = [_site(i, "pinterest.com", f"https://p{i}.example/{i}") for i in range(5)]
        results = parse_yandex_html(_html(sites=low), top_k=3, max_per_domain=1)
        self.assertTrue(results)  # 不为空
        self.assertTrue(all(r.source == "pinterest.com" for r in results))
        self.assertEqual(build_yandex_warnings(results), [SIMILAR_ONLY_WARNING])

    def test_all_low_top_k_filled_when_possible(self):
        # 多个不同 Pinterest 变体被聚合成一个桶，但 max_per_domain=3 时应尽量补足
        low = [_site(i, "pinterest.com", f"https://p{i}.example/{i}") for i in range(5)]
        results = parse_yandex_html(_html(sites=low), top_k=3, max_per_domain=3)
        self.assertEqual(len(results), 3)

    def test_mixed_ordering_high_neutral_low(self):
        sites = [
            _site(0, "pinterest.com", "https://p.example/1"),
            _site(1, "example.com", "https://n.example/1"),
            _site(2, "danbooru.donmai.us", "https://d.example/1"),
        ]
        results = parse_yandex_html(_html(sites=sites), top_k=3)
        self.assertEqual(
            [r.source for r in results],
            ["danbooru.donmai.us", "example.com", "pinterest.com"],
        )

    def test_similar_only_fallback_warns(self):
        similar = [{"title": "s", "linkUrl": "/images/search?cbir_id=1&a=1&b=2&c=3"}]
        results = parse_yandex_html(_html(sites=[], similar=similar))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "yandex-similar")
        self.assertEqual(build_yandex_warnings(results), [SIMILAR_ONLY_WARNING])

    def test_stable_sort_preserves_original_order_within_level(self):
        # 同等级应保持 Yandex 原始出现顺序（稳定排序）
        sites = [
            _site(0, "a-pixiv.net", "https://p/0"),  # 注意：a-pixiv.net 不是 pixiv.net 子域 → neutral
            _site(1, "danbooru.donmai.us", "https://d/1"),
            _site(2, "safebooru.org", "https://s/2"),
            _site(3, "pixiv.net", "https://p/3"),
        ]
        results = parse_yandex_html(_html(sites=sites), top_k=4)
        highs = [r.source for r in results if classify_source(r.source) == LEVEL_HIGH]
        self.assertEqual(highs, ["danbooru.donmai.us", "safebooru.org", "pixiv.net"])


class TestWarningsDoubleEmoji(unittest.TestCase):
    """诚实提示渲染后不得出现「⚠️ ⚠️」双 emoji。"""

    def test_warning_template_has_no_emoji(self):
        self.assertFalse(SIMILAR_ONLY_WARNING.startswith("⚠️"))

    def test_rendered_warning_single_emoji(self):
        outcome = SourceOutcome(
            results=[SearchResult(title="t", source="pinterest.com", url="https://p/1", extra={"source_level": "low"})],
            warnings=[SIMILAR_ONLY_WARNING],
        )
        blocks = format_outcome(outcome, nsfw_send_image=False, max_results=3, header="【搜图】")
        text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        self.assertNotIn("⚠️ ⚠️", text)
        self.assertEqual(text.count("⚠️"), 1)


class TestFormatterRegressionAdditive(unittest.TestCase):
    """formatter additive 改动：无 source_level 字段的 provider 输出必须逐字不变。

    期望串来自改动前实现（git HEAD），逐字硬编码以防回归。
    """

    def _render(self, result: SearchResult) -> list[dict]:
        return format_outcome(
            SourceOutcome(results=[result]),
            nsfw_send_image=False,
            max_results=3,
            header="【搜图】",
        )

    def test_saucenao_style_line_byte_identical(self):
        r = SearchResult(title="标题", source="pixiv", url="https://pixiv.net/a", score=95.0, extra={})
        # 改动前的逐字期望（SauceNAO 风格）
        self.assertEqual(
            _result_line(1, r),
            "1. 【pixiv】标题 | 相似度 95.0%\n   🔗 https://pixiv.net/a",
        )

    def test_no_source_level_no_label_appended(self):
        for extra in ({}, {"artist": "koma"}, {"author": "x"}, {"page_no": 2}, {"rating": "s"}, {"source_level": None}):
            with self.subTest(extra=extra):
                r = SearchResult(title="t", source="pixiv", url="https://p/a", score=90.0, extra=extra)
                line = _result_line(1, r)
                self.assertNotIn("图库/来源", line)
                self.assertNotIn(" | 相关", line)
                self.assertNotIn(" | 相似图", line)

    def test_yandex_high_appends_label(self):
        r = SearchResult(title="t", source="danbooru.donmai.us", url="https://d/1", score=None, extra={"source_level": "high"})
        self.assertEqual(_result_line(1, r), "1. 【danbooru.donmai.us】t | 图库/来源\n   🔗 https://d/1")

    def test_unknown_level_value_ignored(self):
        r = SearchResult(title="t", source="s", url="https://u", score=1.0, extra={"source_level": "weird"})
        self.assertEqual(_result_line(1, r), "1. 【s】t | 相似度 1.0%\n   🔗 https://u")

    def test_extra_not_dict_does_not_break(self):
        r = SearchResult(title="t", source="s", url="https://u", score=1.0, extra=None)  # type: ignore[arg-type]
        # extra 非 dict 时既不走等级标签、也不崩
        line = _result_line(1, r)
        self.assertEqual(line, "1. 【s】t | 相似度 1.0%\n   🔗 https://u")


if __name__ == "__main__":
    unittest.main(verbosity=2)
