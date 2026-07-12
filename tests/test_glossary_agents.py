"""分析器 / 术语抽取 / 滚动上下文 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.glossary.store import (
    GlossaryStore,
    GlossaryTerm,
    TYPE_APPELLATION,
)
from trans_novel.glossary.extractor import (
    GlossaryExtractionError,
    GlossaryExtractor,
)
from trans_novel.agents.analyzer import Analyzer
from trans_novel.pipeline.context import RollingContext


def _cfg():
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
    })


class TestAnalyzer(unittest.TestCase):
    def test_analyze_and_seed(self):
        analysis = {
            "genre": "校园", "tone": "冷峻第三人称",
            "style_guide": "保持克制",
            "characters": [{"source": "綾小路", "target": "绫小路",
                            "gender": "男", "reading": "あやのこうじ", "note": "第一人称用俺"}],
            "terms": [{"source": "高度育成高校", "target": "高度育成高中", "type": "组织"}],
        }
        client = FakeClient(handler=lambda m, t, j: json.dumps(analysis, ensure_ascii=False))
        a = Analyzer(client, _cfg())
        result = a.analyze("……样章……")
        self.assertEqual(result["genre"], "校园")

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            n = a.seed_glossary(store, result)
            self.assertEqual(n, 2)
            self.assertEqual(store.get_term("綾小路").gender, "男")
            self.assertEqual(store.get_term("高度育成高校").type, "组织")
            store.close()

        brief = a.style_brief(result)
        self.assertIn("绫小路", brief)

    def test_malformed_collections_and_fields_are_sanitized(self):
        payload = {
            "genre": ["not", "text"],
            "characters": [
                "not-an-object",
                {"source": ["bad"], "target": "跳过"},
                {"source": " Valid ", "target": " 有效 ", "gender": []},
            ],
            "terms": 7,
        }
        analyzer = Analyzer(
            FakeClient(handler=lambda m, t, j: json.dumps(payload)), _cfg()
        )
        result = analyzer.analyze("sample")
        self.assertEqual(result["genre"], "")
        self.assertEqual(len(result["characters"]), 2)
        self.assertEqual(result["terms"], [])

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            self.assertEqual(analyzer.seed_glossary(store, result), 1)
            term = store.get_term("Valid")
            assert term is not None
            self.assertEqual(term.target, "有效")
            self.assertEqual(term.gender, "")
            store.close()
        self.assertIn("有效", analyzer.style_brief(result))


class TestExtractor(unittest.TestCase):
    def test_extract_and_store(self):
        terms = {"terms": [
            {"source": "堀北", "target": "堀北", "type": "人物", "gender": "女",
             "aliases": ["堀北さん"]},
            {"source": "屋上", "target": "天台", "type": "地名", "gender": "未知"},
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(terms, ensure_ascii=False))
        ext = GlossaryExtractor(client, _cfg())
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = ext.extract_and_store(store, "原文", "译文", chapter=1)
            self.assertEqual(summary["inserted"], 2)
            horikita = store.get_term("堀北")
            self.assertEqual(horikita.gender, "女")
            self.assertEqual(horikita.aliases, ["堀北さん"])
            self.assertEqual(horikita.first_chapter, 1)
            # "未知" 应被规整为空
            self.assertEqual(store.get_term("屋上").gender, "")
            store.close()

    def test_extract_and_store_normalizes_non_string_optional_fields(self):
        malformed_values = [
            ("list", ["unexpected"]),
            ("dict", {"unexpected": "value"}),
            ("null", None),
            ("number", 7),
        ]
        payload = {"terms": [
            {
                "source": f"source-{label}",
                "target": f"target-{label}",
                "reading": value,
                "type": value,
                "gender": value,
                "aliases": [],
                "note": value,
            }
            for label, value in malformed_values
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(payload))
        ext = GlossaryExtractor(client, _cfg())

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = ext.extract_and_store(store, "source", "target", chapter=3)
            self.assertEqual(summary["inserted"], len(malformed_values))
            for label, _ in malformed_values:
                with self.subTest(value_type=label):
                    term = store.get_term(f"source-{label}")
                    assert term is not None
                    self.assertEqual(term.reading, "")
                    self.assertEqual(term.type, "术语")
                    self.assertEqual(term.gender, "")
                    self.assertEqual(term.aliases, [])
                    self.assertEqual(term.note, "")
                    self.assertEqual(term.first_chapter, 3)
            store.close()

    def test_extract_normalizes_aliases(self):
        alias_cases = [
            ("string", "single-alias", []),
            ("dict", {"alias": "value"}, []),
            ("null", None, []),
            ("number", 7, []),
            ("mixed-list", [" alias-a ", "", None, 9, {}, "alias-b"],
             ["alias-a", "alias-b"]),
        ]
        payload = {"terms": [
            {
                "source": f"source-{label}",
                "target": f"target-{label}",
                "aliases": aliases,
            }
            for label, aliases, _ in alias_cases
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(payload))
        ext = GlossaryExtractor(client, _cfg())

        terms = {term.source: term for term in ext.extract("source", "target", [])}
        self.assertEqual(len(terms), len(alias_cases))
        for label, _, expected in alias_cases:
            with self.subTest(value_type=label):
                self.assertEqual(terms[f"source-{label}"].aliases, expected)

    def test_extract_skips_invalid_required_fields(self):
        malformed_values = [["unexpected"], {"unexpected": "value"}, None, 7, "   "]
        payload = {"terms": [
            {"source": value, "target": "valid-target"}
            for value in malformed_values
        ] + [
            {"source": "valid-source", "target": value}
            for value in malformed_values
        ] + [
            {"source": " kept ", "target": " translated "},
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(payload))
        ext = GlossaryExtractor(client, _cfg())

        terms = ext.extract("source", "target", [])
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].source, "kept")
        self.assertEqual(terms[0].target, "translated")

    def test_strict_response_envelope_distinguishes_empty_success(self):
        invalid = [
            ("top-level-list", []),
            ("missing-key", {}),
            ("terms-object", {"terms": {}}),
            ("terms-null", {"terms": None}),
        ]
        for label, payload in invalid:
            with self.subTest(case=label):
                ext = GlossaryExtractor(
                    FakeClient(handler=lambda m, t, j, p=payload: json.dumps(p)),
                    _cfg(),
                )
                with self.assertRaises(GlossaryExtractionError):
                    ext.extract("source", "target", [])

        ext = GlossaryExtractor(
            FakeClient(handler=lambda m, t, j: json.dumps({"terms": []})),
            _cfg(),
        )
        self.assertEqual(ext.extract("source", "target", []), [])

    def test_store_extraction_prompt_uses_only_source_relevant_terms(self):
        cfg = _cfg()
        cfg.pipeline.glossary_scope = "full"
        client = FakeClient(
            handler=lambda m, t, j: json.dumps({"terms": []})
        )
        ext = GlossaryExtractor(client, cfg)
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            store.upsert_term(GlossaryTerm(source="alpha", target="甲"))
            store.upsert_term(
                GlossaryTerm(
                    source="CanonicalAlias",
                    target="别名命中",
                    aliases=["AliasHit"],
                )
            )
            store.upsert_term(
                GlossaryTerm(
                    source="Appellation",
                    target="称谓",
                    aliases=["AliasHit"],
                    type=TYPE_APPELLATION,
                )
            )
            store.upsert_term(GlossaryTerm(source="pha", target="错误子串"))
            store.upsert_term(GlossaryTerm(source="李", target="李"))
            for index in range(100):
                store.upsert_term(
                    GlossaryTerm(source=f"noise-{index}", target=f"噪声-{index}")
                )

            summary = ext.extract_and_store(
                store,
                "alpha met AliasHit. 李さん arrived.",
                "甲遇到了别名命中。李到了。",
                chapter=2,
            )
            user = client.calls[-1]["messages"][-1]["content"]
            self.assertIn("CanonicalAlias", user)
            self.assertIn("alpha", user)
            self.assertIn("李", user)
            self.assertNotIn("Appellation", user)
            self.assertNotIn("错误子串", user)
            self.assertNotIn("noise-99", user)
            self.assertEqual(summary["reference_terms_selected"], 3)
            self.assertEqual(summary["reference_terms_total"], 105)
            store.close()


class TestRollingContext(unittest.TestCase):
    def test_render_and_bound(self):
        ctx = RollingContext(max_recent_keep=3)
        ctx.add_targets(["a", "b", "c", "d", "e"])
        self.assertEqual(ctx.recent_targets, ["c", "d", "e"])  # 限长
        rendered = ctx.render(n_recent=2)  # 只取最近两段
        self.assertIn("d", rendered)
        self.assertIn("e", rendered)
        self.assertNotIn("c", rendered)

    def test_roundtrip(self):
        ctx = RollingContext(recent_targets=["x", "y"])
        ctx2 = RollingContext.from_dict(ctx.to_dict())
        self.assertEqual(ctx2.recent_targets, ["x", "y"])


if __name__ == "__main__":
    unittest.main()
