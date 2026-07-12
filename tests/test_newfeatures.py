"""新功能测试（离线）：模型语言检测、标点规范化、术语 AI 审计统一、连续全流程。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from trans_novel.config import Config
from trans_novel.postprocess.punct import normalize_zh
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


class TestModelLanguageDetection(unittest.TestCase):
    def _cfg(self, state: str) -> Config:
        return Config.from_dict({
            "language": {"source": "auto", "target": "zh"},
            "llm": {"provider": "fake", "tiers": {
                "strong": {"model": "p"}, "cheap": {"model": "f"}}},
            "pipeline": {"book_understanding": False},
            "paths": {"state_dir": state},
        })

    def test_auto_uses_model_detection(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": "russian"}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            store = Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)
            self.assertEqual(cfg.source_lang, "ru")
            self.assertEqual(store.load_manifest()["source_lang"], "ru")

    def test_auto_detection_failure_requires_user_source(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": ""}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            with self.assertRaisesRegex(RuntimeError, "language.source"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)


class TestPunct(unittest.TestCase):
    def test_japanese_quotes(self):
        self.assertEqual(normalize_zh("「你好」"), "“你好”")
        self.assertEqual(normalize_zh("『书名』"), "‘书名’")

    def test_halfwidth_to_full_in_cjk(self):
        self.assertEqual(normalize_zh("他说,真的吗?"), "他说，真的吗？")

    def test_no_harm_to_english_numbers(self):
        self.assertEqual(normalize_zh("9.11 vs 9.8"), "9.11 vs 9.8")

    def test_ellipsis_and_dash(self):
        self.assertEqual(normalize_zh("等等...走了--他笑了"), "等等……走了——他笑了")


class TestGlossaryAudit(unittest.TestCase):
    def test_decide_rejects_malformed_and_out_of_candidate_values(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor

        cfg = Config.from_dict({
            "language": {"source": "ja", "target": "zh"},
            "llm": {"provider": "fake", "tiers": {
                "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        })
        candidates = {
            "カホ": {
                "source": "カホ",
                "current": "佳穂",
                "type": "人物",
                "variants": ["佳穗"],
            }
        }
        response = {"unifications": [
            {"source": {"bad": True}, "canonical": "佳穂"},
            {"source": "UNKNOWN", "canonical": "伪造"},
            {"source": "カホ", "canonical": {"bad": True}},
            {"source": "カホ", "canonical": "新造译法"},
            {
                "source": "カホ",
                "canonical": "佳穂",
                "variants": ["佳穗", None, {"bad": True}, "任意替换"],
                "reason": {"bad": True},
            },
        ]}
        auditor = GlossaryAuditor(
            FakeClient(handler=lambda m, t, j: json.dumps(
                response, ensure_ascii=False
            )),
            cfg,
        )
        self.assertEqual(auditor._decide(candidates), [{
            "source": "カホ",
            "canonical": "佳穂",
            "variants": ["佳穗"],
            "reason": "",
        }])

    def test_unify_variants_and_rewrite_targets(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict({
                "language": {"source": "ja", "target": "zh"},
                "llm": {"provider": "fake", "tiers": {
                    "strong": {"model": "p"}, "cheap": {"model": "f"}}},
                "pipeline": {"review": False, "polish": False,
                             "backtranslate_sample": 0.0, "consistency_qa": False},
                "paths": {"state_dir": state},
            })
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            store = orch.run(txt)

            # 人为制造译法漂移：术语表写入 佳穂，章节正文里混入 佳穗
            g = GlossaryStore(store.glossary_path)
            g.upsert_term(GlossaryTerm(source="カホ", target="佳穂", type="人物"), chapter=0)
            g.close()
            ch = store.load_chapter(0)
            ch.segments[1].target = "佳穂和佳穗在一起。"  # 同名两种写法
            ch.meta.pop("glossary_plan", None)
            store.save_chapter(ch)
            manifest = store.load_manifest()
            manifest["chapters"][0].pop("glossary_status")
            manifest["chapters"][0]["glossary_legacy"] = True
            store.save_manifest(manifest)

            def handler(messages, tier, json_mode):
                if "术语一致性审计员" in messages[0]["content"]:
                    return json.dumps({"unifications": [
                        {"source": "カホ", "canonical": "佳穂",
                         "variants": ["佳穗"], "reason": "统一为佳穂"}
                    ]}, ensure_ascii=False)
                return "{}"

            g = GlossaryStore(store.glossary_path)
            applied = GlossaryAuditor(FakeClient(handler=handler), cfg).audit(store, g)
            self.assertEqual(len(applied), 1)
            term = g.get_term("カホ")
            self.assertTrue(term.locked)
            self.assertIn("佳穗", term.aliases)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                "pending",
            )
            self.assertEqual(store.load_manifest()["titles_status"], "pending")
            self.assertNotIn(
                "glossary_legacy",
                store.load_manifest()["chapters"][0],
            )
            self.assertNotIn(
                "title_translated",
                store.load_manifest()["chapters"][0],
            )

            from trans_novel.glossary.store import GlossaryCheckpoint
            from trans_novel.pipeline.runstore import translation_fingerprint

            rewritten = store.load_chapter(0)
            final_checkpoint = GlossaryCheckpoint(
                scope="chapter",
                chapter=0,
                start_index=0,
                count=len(rewritten.text_segments),
                fingerprint=translation_fingerprint(rewritten.text_segments),
            )
            self.assertFalse(g.checkpoint_matches(final_checkpoint))
            g.close()

            # 正文里的 佳穗 应已被改写为 佳穂
            ch2 = store.load_chapter(0)
            self.assertEqual(ch2.segments[1].target, "佳穂和佳穂在一起。")

            resume_client = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=resume_client).run(txt)
            self.assertFalse(any(
                "文学翻译" in call["messages"][0]["content"]
                for call in resume_client.calls
            ))
            g = store.open_glossary()
            self.assertTrue(g.checkpoint_matches(final_checkpoint))
            g.close()
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                "done",
            )


class TestRunAll(unittest.TestCase):
    def test_continuous_pipeline_outputs_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict({
                "language": {"source": "auto", "target": "zh"},
                "llm": {"provider": "fake", "tiers": {
                    "strong": {"model": "p"}, "cheap": {"model": "f"}}},
                "pipeline": {"review": True, "polish": True,
                             "backtranslate_sample": 0.0, "consistency_qa": True},
                "paths": {"state_dir": state},
            })
            seen = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(
                txt, progress=lambda done, total, label: seen.append((done, total)),
                out_format="epub",
            )
            self.assertTrue(result["output"].endswith(".epub"))
            self.assertTrue(zipfile.is_zipfile(result["output"]))
            # 进度回调被触发，且最终 done==total
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])
            # auto 通过模型检测把源语言定为 ja
            self.assertEqual(cfg.source_lang, "ja")
            # 报告含一致性字段；术语审计不在连续流程中自动运行
            self.assertIn("consistency_issues", result["report"])
            with open(result["store"].event_log_path, "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            event_names = [e["event"] for e in events]
            self.assertIn("run_initialized", event_names)
            self.assertIn("batch_translated", event_names)
            self.assertIn("report_saved", event_names)
            self.assertIn("assembled", event_names)
            self.assertNotIn("glossary_audit_finished", event_names)
            translated = next(e for e in events if e["event"] == "batch_translated")
            self.assertTrue(translated["segments"])
            self.assertIn("source", translated["segments"][0])
            self.assertIn("target", translated["segments"][0])


if __name__ == "__main__":
    unittest.main()
