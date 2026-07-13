"""翻译 agent 的对齐保证测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import re
import unittest

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.agents.translator import Translator
from trans_novel.pipeline.checks import count_aligned, length_flags


def _count_segments(user_content: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", user_content, re.M))


class TestTranslatorAlignment(unittest.TestCase):
    def _config(self):
        return Config.from_dict({
            "language": {"source": "ja", "target": "zh"},
            "llm": {"provider": "fake", "tiers": {
                "strong": {"model": "deepseek-v4-pro"},
                "cheap": {"model": "deepseek-v4-flash"},
            }},
            "pipeline": {"align_retry_limit": 1},
        })

    def test_happy_path_aligned(self):
        def handler(messages, tier, json_mode):
            n = _count_segments(messages[-1]["content"])
            return json.dumps({"translations": [f"译{i}" for i in range(n)]},
                              ensure_ascii=False)

        t = Translator(FakeClient(handler=handler), self._config())
        out = t.translate_batch(["あ", "い", "う"])
        self.assertEqual(len(out), 3)
        self.assertEqual(out, ["译0", "译1", "译2"])

    def test_fallback_to_per_segment_on_mismatch(self):
        # 多段批次故意少返回一段；单段调用正常 → 触发逐段兜底
        def handler(messages, tier, json_mode):
            n = _count_segments(messages[-1]["content"])
            trans = [f"译{i}" for i in range(n)]
            if n > 1:
                trans = trans[:-1]  # 故意制造段数不符
            return json.dumps({"translations": trans}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        t = Translator(client, self._config())
        out = t.translate_batch(["あ", "い", "う"])
        self.assertEqual(len(out), 3)  # 兜底后仍保证 1:1
        # 验证确实回退到了逐段（出现过 n==1 的调用）
        single_calls = [c for c in client.calls
                        if _count_segments(c["messages"][-1]["content"]) == 1]
        self.assertGreaterEqual(len(single_calls), 3)

    def test_per_segment_fallback_keeps_alignment_when_single_call_fails(self):
        def handler(messages, tier, json_mode):
            raise RuntimeError("model down")

        t = Translator(FakeClient(handler=handler), self._config())
        out = t.translate_batch(["あ", "い"])
        self.assertEqual(out, ["", ""])

    def test_aligned_response_with_empty_item_falls_back_per_segment(self):
        def handler(messages, tier, json_mode):
            n = _count_segments(messages[-1]["content"])
            if n > 1:
                return json.dumps(
                    {"translations": ["批译"] + [""] * (n - 1)},
                    ensure_ascii=False,
                )
            return json.dumps({"translations": ["单段补译"]}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        out = Translator(client, self._config()).translate_batch(["あ", "い"])
        self.assertEqual(out, ["单段补译", "单段补译"])
        self.assertGreaterEqual(sum(
            _count_segments(call["messages"][-1]["content"]) == 1
            for call in client.calls
        ), 2)

    def test_non_string_translation_items_are_protocol_failures(self):
        calls = 0

        def handler(messages, tier, json_mode):
            nonlocal calls
            calls += 1
            n = _count_segments(messages[-1]["content"])
            if n > 1:
                return json.dumps(
                    {"translations": [None, {"bad": True}][:n]},
                    ensure_ascii=False,
                )
            return json.dumps({"translations": ["单段安全译文"]}, ensure_ascii=False)

        out = Translator(FakeClient(handler=handler), self._config()).translate_batch(
            ["あ", "い"]
        )
        self.assertEqual(out, ["单段安全译文", "单段安全译文"])
        self.assertGreaterEqual(calls, 4)

    def test_retranslate_rejects_non_string_item(self):
        client = FakeClient(handler=lambda m, t, j: json.dumps(
            {"translations": [None]}, ensure_ascii=False))
        out = Translator(client, self._config()).retranslate_with_feedback(
            "原文", feedback="修复"
        )
        self.assertEqual(out, "")


class TestChecks(unittest.TestCase):
    def test_count_aligned(self):
        self.assertTrue(count_aligned(["a", "b"], ["甲", "乙"]))
        self.assertFalse(count_aligned(["a", "b"], ["甲"]))

    def test_length_flags(self):
        sources = ["これは長い日本語の文章です。" * 3, "短い", "x" * 10]
        targets = ["", "短い但正常的中文译文内容", "x" * 40]
        flags = length_flags(sources, targets)
        kinds = {f.index: f.reason for f in flags}
        self.assertEqual(kinds.get(0), "empty")     # 译文为空
        self.assertEqual(kinds.get(2), "too_long")  # 比值过大


if __name__ == "__main__":
    unittest.main()
