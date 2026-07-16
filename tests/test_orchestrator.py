"""编排器端到端 + 断点续跑测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator, _normalize_lang
from trans_novel.pipeline.runstore import RunStore, STATUS_DONE, STATUS_PENDING, slugify
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


def _translated_para_count(calls) -> int:
    """统计送进翻译模型的源段总数（按编号行计）。"""
    n = 0
    for c in calls:
        if "文学翻译" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.M))
    return n


def _config(state_dir: str):
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        "segment": {"max_chars_per_batch": 1800},
        "pipeline": {"review": True, "polish": True,
                     "backtranslate_sample": 0.0, "consistency_qa": True},
        "paths": {"state_dir": state_dir},
    })


class TestOrchestrator(unittest.TestCase):
    def test_full_run_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)

            # 全部章节标记 done
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m["chapters"]))

            # 每段都有译文（润色后为 "润{i}"）
            ch0 = store.load_chapter(0)
            self.assertTrue(all(s.target for s in ch0.text_segments))

            # 术语抽取写入了「堀北」；分析器种入了「绫小路」
            from trans_novel.glossary.store import GlossaryStore
            g = GlossaryStore(store.glossary_path)
            self.assertIsNotNone(g.get_term("綾小路"))
            self.assertIsNotNone(g.get_term("堀北"))
            self.assertGreater(g.stats()["tm_entries"], 0)  # 翻译记忆库已写入
            g.close()

            # ── 续跑：所有章已 done，不应再产生翻译调用 ──
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            orch2.run(txt)  # resume 语义
            translate_calls = [c for c in client2.calls
                               if "文学翻译" in c["messages"][0]["content"]]
            self.assertEqual(len(translate_calls), 0)

    def test_resume_after_partial(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            # 只翻第 0 章
            store = orch.run(txt, only_chapter=0)
            m = store.load_manifest()
            self.assertEqual(m["chapters"][0]["status"], STATUS_DONE)
            self.assertNotEqual(m["chapters"][1]["status"], STATUS_DONE)

            # 续跑应只补翻第 1 章
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            store2 = orch2.run(txt)
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))

    def test_prepare_continues_when_analysis_returns_non_json(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "前期分析师" in messages[0]["content"]:
                    return "你好，我无法给到相关内容。"
                return routing_handler(messages, tier, json_mode)

            store = Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)
            self.assertEqual(store.load_analysis(), {})
            self.assertEqual(store.load_context(), {"recent_targets": []})
            with open(store.event_log_path, "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            names = [e["event"] for e in events]
            self.assertIn("analysis_failed", names)
            self.assertIn("analysis_saved", names)

    def test_prepare_repairs_existing_run_missing_analysis_and_context(self):
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            doc = load_document(txt, "ja", "zh")
            run_dir = os.path.join(cfg.state_dir, slugify(doc.title))
            store = RunStore(run_dir)
            from trans_novel.glossary.store import GlossaryStore
            glossary = GlossaryStore(store.glossary_path)
            store.init_from_document(
                doc,
                glossary_generation_id=glossary.generation_id,
            )
            glossary.close()
            self.assertFalse(os.path.exists(store.analysis_path))
            self.assertFalse(os.path.exists(store.context_path))

            repaired = Orchestrator(
                cfg,
                client=FakeClient(handler=routing_handler),
            ).prepare(txt)

            self.assertTrue(os.path.exists(repaired.analysis_path))
            self.assertTrue(os.path.exists(repaired.context_path))
            self.assertTrue((repaired.load_analysis() or {}).get("genre"))
            with open(repaired.event_log_path, "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertIn("analysis_repaired", [e["event"] for e in events])

    def test_prepare_fails_closed_when_existing_glossary_database_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).run(txt)
            os.remove(store.glossary_path)

            with self.assertRaisesRegex(FileNotFoundError, "database is missing"):
                Orchestrator(
                    cfg, client=FakeClient(handler=routing_handler)
                ).prepare(txt)
            self.assertFalse(os.path.exists(store.glossary_path))

    def test_prepare_fails_closed_when_glossary_database_is_replaced(self):
        from trans_novel.glossary.store import (
            GlossaryStore,
            GlossaryStoreIdentityError,
        )

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).run(txt)
            expected = store.load_manifest()["glossary_generation_id"]
            os.replace(store.glossary_path, store.glossary_path + ".owned")
            replacement = GlossaryStore(store.glossary_path)
            actual = replacement.generation_id
            replacement.close()
            self.assertNotEqual(actual, expected)

            with self.assertRaisesRegex(
                GlossaryStoreIdentityError, "generation mismatch"
            ):
                Orchestrator(
                    cfg, client=FakeClient(handler=routing_handler)
                ).prepare(txt)

    def test_new_manifest_missing_generation_rejects_replacement_database(self):
        from trans_novel.glossary.store import (
            GlossaryStore,
            GlossaryStoreIdentityError,
        )

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).run(txt)
            manifest = store.load_manifest()
            manifest.pop("glossary_generation_id")
            store.save_manifest(manifest)
            os.replace(store.glossary_path, store.glossary_path + ".owned")
            replacement = GlossaryStore(store.glossary_path)
            replacement.close()

            with self.assertRaisesRegex(
                GlossaryStoreIdentityError,
                "new-format manifest is missing glossary_generation_id",
            ):
                Orchestrator(
                    cfg, client=FakeClient(handler=routing_handler)
                ).prepare(txt)

    def test_positive_legacy_manifest_binds_existing_database_once(self):
        from trans_novel.glossary.store import GlossaryStore
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            doc = load_document(txt, "ja", "zh")
            store = RunStore(os.path.join(cfg.state_dir, slugify(doc.title)))
            glossary = GlossaryStore(store.glossary_path)
            generation = glossary.generation_id
            glossary.close()
            store.init_from_document(doc, glossary_generation_id=generation)
            manifest = store.load_manifest()
            for key in (
                "state_format_version",
                "analysis_glossary_status",
                "titles_status",
                "glossary_generation_id",
            ):
                manifest.pop(key)
            for entry in manifest["chapters"]:
                entry.pop("glossary_status")
            store.save_manifest(manifest)

            glossary = store.open_glossary()
            glossary.close()
            rebound = store.load_manifest()
            self.assertEqual(rebound["glossary_generation_id"], generation)
            self.assertEqual(rebound["state_format_version"], 1)
            self.assertTrue(all(
                entry.get("glossary_legacy") is True
                for entry in rebound["chapters"]
            ))

    def test_migrated_legacy_markers_prevent_second_database_binding(self):
        from trans_novel.glossary.store import (
            GlossaryStore,
            GlossaryStoreIdentityError,
        )
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            doc = load_document(txt, "ja", "zh")
            store = RunStore(os.path.join(cfg.state_dir, slugify(doc.title)))
            glossary = GlossaryStore(store.glossary_path)
            generation = glossary.generation_id
            glossary.close()
            store.init_from_document(doc, glossary_generation_id=generation)
            manifest = store.load_manifest()
            for key in (
                "state_format_version",
                "analysis_glossary_status",
                "titles_status",
                "glossary_generation_id",
            ):
                manifest.pop(key)
            for entry in manifest["chapters"]:
                entry.pop("glossary_status")
            store.save_manifest(manifest)
            store.open_glossary().close()

            migrated = store.load_manifest()
            migrated.pop("state_format_version")
            migrated.pop("glossary_generation_id")
            migrated["chapters"][0]["glossary_legacy"] = False
            store.save_manifest(migrated)
            os.replace(store.glossary_path, store.glossary_path + ".owned")
            replacement = GlossaryStore(store.glossary_path)
            replacement.close()

            with self.assertRaisesRegex(
                GlossaryStoreIdentityError,
                "new-format manifest is missing glossary_generation_id",
            ):
                store.open_glossary()

    def test_null_plan_prevents_pristine_legacy_database_adoption(self):
        from trans_novel.glossary.store import (
            GlossaryStore,
            GlossaryStoreIdentityError,
        )
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            doc = load_document(txt, "ja", "zh")
            store = RunStore(os.path.join(cfg.state_dir, slugify(doc.title)))
            glossary = GlossaryStore(store.glossary_path)
            generation = glossary.generation_id
            glossary.close()
            store.init_from_document(doc, glossary_generation_id=generation)
            manifest = store.load_manifest()
            for key in (
                "state_format_version",
                "analysis_glossary_status",
                "titles_status",
                "glossary_generation_id",
            ):
                manifest.pop(key)
            for entry in manifest["chapters"]:
                entry.pop("glossary_status")
            store.save_manifest(manifest)
            chapter = store.load_chapter(0)
            chapter.meta["glossary_plan"] = None
            store.save_chapter(chapter)

            with self.assertRaisesRegex(
                GlossaryStoreIdentityError,
                "new-format manifest is missing glossary_generation_id",
            ):
                store.open_glossary()

    def test_prepare_fails_closed_on_orphan_database_without_manifest(self):
        from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            doc = load_document(txt, "ja", "zh")
            store = RunStore(os.path.join(cfg.state_dir, slugify(doc.title)))
            orphan = GlossaryStore(store.glossary_path)
            orphan.upsert_term(
                GlossaryTerm(
                    source="POISON",
                    target="污染",
                    confidence="high",
                    locked=True,
                )
            )
            orphan.close()

            with self.assertRaisesRegex(RuntimeError, "缺少 manifest.json"):
                Orchestrator(
                    cfg, client=FakeClient(handler=routing_handler)
                ).prepare(txt)
            self.assertFalse(store.exists())
            orphan = GlossaryStore(store.glossary_path, create=False)
            self.assertIsNotNone(orphan.get_term("POISON"))
            orphan.close()

    def test_analysis_is_saved_before_seed_and_status_done(self):
        """A failed analysis save must leave SQLite unseeded and status pending."""
        from trans_novel.glossary.store import GlossaryStore
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            with patch.object(
                RunStore,
                "save_analysis",
                side_effect=OSError("analysis disk failure"),
            ):
                with self.assertRaisesRegex(OSError, "analysis disk failure"):
                    Orchestrator(
                        cfg, client=FakeClient(handler=routing_handler)
                    ).prepare(txt)

            doc = load_document(txt, "ja", "zh")
            run_dir = os.path.join(cfg.state_dir, slugify(doc.title))
            store = RunStore(run_dir)
            self.assertEqual(
                store.load_manifest()["analysis_glossary_status"],
                STATUS_PENDING,
            )
            glossary = GlossaryStore(store.glossary_path)
            self.assertEqual(glossary.stats()["terms"], 0)
            glossary.close()
            self.assertFalse(os.path.exists(store.analysis_path))

    def test_prepare_retries_saved_analysis_seed_after_storage_failure(self):
        """analysis 已保存但原子 seed 失败时，续跑从文件重试且不再请求 analyzer。"""
        from trans_novel.glossary.store import GlossaryStore
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            with patch.object(
                GlossaryStore,
                "upsert_terms",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    Orchestrator(
                        cfg, client=FakeClient(handler=routing_handler)
                    ).prepare(txt)

            doc = load_document(txt, "ja", "zh")
            store = RunStore(os.path.join(cfg.state_dir, slugify(doc.title)))
            self.assertTrue((store.load_analysis() or {}).get("characters"))
            self.assertEqual(
                store.load_manifest()["analysis_glossary_status"],
                STATUS_PENDING,
            )

            def no_reanalysis(messages, tier, json_mode):
                if "前期分析师" in messages[0]["content"]:
                    raise AssertionError("saved analysis should be reused")
                return routing_handler(messages, tier, json_mode)

            repaired = Orchestrator(
                cfg, client=FakeClient(handler=no_reanalysis)
            ).prepare(txt)
            self.assertEqual(
                repaired.load_manifest()["analysis_glossary_status"],
                STATUS_DONE,
            )
            glossary = GlossaryStore(repaired.glossary_path)
            self.assertIsNotNone(glossary.get_term("綾小路"))
            glossary.close()
            with open(repaired.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertIn(
                "analysis_glossary_seed_repaired",
                [event["event"] for event in events],
            )


class TestSegmentLevelResume(unittest.TestCase):
    def _tr_handler(self, tag):
        """返回带标记的翻译 handler（译文形如 {tag}译{i}），其余走默认路由。"""
        def handler(messages, tier, json_mode):
            if "文学翻译" in messages[0]["content"]:
                n = len(re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.M))
                return json.dumps({"translations": [f"{tag}译{i}" for i in range(n)]},
                                  ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)
        return handler

    def test_resume_skips_done_segments_keeps_their_text(self):
        """中断后续跑：已译完的段原样保留、不重翻；只补译未完成的段。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8     # 每段≈独立批，便于精确续跑
            cfg.pipeline.polish = False             # 保留翻译标记，便于断言（与续跑无关）

            # 第一次：用 R1 译完第 0 章
            c1 = FakeClient(handler=self._tr_handler("R1"))
            store = Orchestrator(cfg, client=c1).run(txt, only_chapter=0)
            ch = store.load_chapter(0)
            self.assertTrue(all(s.target and s.target.startswith("R1") for s in ch.text_segments))

            # 模拟中断：清空最后一段译文、章状态改回 pending
            ch.segments[-1].target = ""
            store.save_chapter(ch)
            store.set_chapter_status(0, STATUS_PENDING)

            # 第二次：用 R2 续跑——只应补译被清空的那 1 段
            c2 = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=c2).run(txt, only_chapter=0)
            self.assertEqual(_translated_para_count(c2.calls), 1)   # 仅 1 段被重翻

            ch2 = store.load_chapter(0)
            # 之前已译的段仍是 R1（未被跨位置复用、也未重翻），补译段是 R2
            self.assertTrue(ch2.text_segments[0].target.startswith("R1"))
            self.assertTrue(ch2.text_segments[-1].target.startswith("R2"))

    def test_legacy_done_chapter_with_hole_reopens_only_missing_segment(self):
        """Legacy done+empty is migrated without changing saved translations."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review = False
            cfg.pipeline.polish = False
            cfg.pipeline.book_understanding = False
            store = Orchestrator(
                cfg, client=FakeClient(handler=self._tr_handler("R1"))
            ).run(txt)
            chapter = store.load_chapter(0)
            preserved = chapter.text_segments[0].target
            chapter.text_segments[1].target = ""
            chapter.meta.pop("glossary_plan", None)
            store.save_chapter(chapter)
            manifest = store.load_manifest()
            manifest["chapters"][0].pop("glossary_status", None)
            manifest["chapters"][0]["glossary_legacy"] = True
            manifest["chapters"][0]["status"] = STATUS_DONE
            store.save_manifest(manifest)

            client = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=client).run(txt)
            repaired = store.load_chapter(0)
            self.assertEqual(_translated_para_count(client.calls), 1)
            self.assertEqual(repaired.text_segments[0].target, preserved)
            self.assertEqual(repaired.text_segments[1].target, "R2译0")
            self.assertEqual(
                store.load_manifest()["chapters"][0]["status"], STATUS_DONE
            )
            with open(store.event_log_path, encoding="utf-8") as f:
                names = [json.loads(line)["event"] for line in f if line.strip()]
            self.assertIn("legacy_chapter_translation_reopened", names)

    def test_partial_resume_autofix_cannot_overwrite_saved_target(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.book_understanding = False
            cfg.pipeline.autofix_severe = True
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).prepare(txt)
            chapter = store.load_chapter(0)
            preserved = "  已保存译文，不得覆盖。  "
            chapter.text_segments[0].target = preserved
            store.save_chapter(chapter)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "译文审校" in system:
                    return json.dumps({"issues": [{
                        "index": 0,
                        "type": "missing",
                        "detail": "要求覆盖旧段",
                        "suggestion": "重译",
                    }]}, ensure_ascii=False)
                if "文学翻译" in system and "【审校意见】" in user:
                    return json.dumps(
                        {"translations": ["不应采用的覆盖译文"]},
                        ensure_ascii=False,
                    )
                return self._tr_handler("R2")(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(txt, only_chapter=0)
            repaired = store.load_chapter(0)
            self.assertEqual(repaired.text_segments[0].target, preserved)
            self.assertFalse(any(
                "文学翻译" in call["messages"][0]["content"]
                and "【审校意见】" in call["messages"][-1]["content"]
                for call in client.calls
            ))
            saved_issue = next(
                issue for issue in repaired.meta["review_issues"]
                if issue.get("detail") == "要求覆盖旧段"
            )
            self.assertFalse(saved_issue["fixed"])
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(
                event["event"] == "autofix_skipped"
                and event.get("reason") == "saved_target_immutable"
                for event in events
            ))

    def test_context_is_rebuilt_from_done_chapters_not_stale_cache(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review = False
            cfg.pipeline.polish = False
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).run(txt)
            store.save_context({"recent_targets": ["STALE-CONTEXT"]})

            rebuilt = Orchestrator._context_before_chapter(store, 1)
            chapter_zero_targets = [
                segment.target for segment in store.load_chapter(0).text_segments
            ]
            self.assertNotIn("STALE-CONTEXT", rebuilt.recent_targets)
            for target in chapter_zero_targets:
                self.assertIn(target, rebuilt.recent_targets)


class TestBookUnderstanding(unittest.TestCase):
    def _translate_user(self, calls) -> str:
        """返回最后一次翻译调用送进模型的 user 文本。"""
        for c in reversed(calls):
            if "文学翻译" in c["messages"][0]["content"]:
                return c["messages"][-1]["content"]
        return ""

    def test_prepass_builds_and_injects(self):
        """预扫产出逐章梗概+全书概览，并注入翻译 prompt。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            # 逐章梗概落盘到 chapter.meta
            self.assertTrue(store.load_chapter(0).meta.get("source_digest"))
            # 全书概览落盘到 analysis
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))

            # 翻译 prompt 注入了全书概览 / 本章梗概块（且非「（无）」占位）
            user = self._translate_user(client.calls)
            self.assertIn("【全书概览】", user)
            self.assertIn("【本章梗概】", user)
            self.assertIn("全书概览", user)   # fake 概览正文
            self.assertIn("本章梗概", user)   # fake 逐章梗概正文

    def test_prescan_parallel(self):
        """并行预扫：多线程 digest 后各章梗概按章序落盘，翻译注入正常。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.prescan_concurrency = 3

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            m = store.load_manifest()
            for c in m["chapters"]:
                self.assertTrue(store.load_chapter(c["index"]).meta.get("source_digest"))
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            user = self._translate_user(client.calls)
            self.assertIn("【本章梗概】", user)

    def test_resume_skips_prepass(self):
        """续跑：梗概/概览已落盘，不再产生预扫调用。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(txt)

            c2 = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=c2).run(txt)
            prepass = [c for c in c2.calls
                       if "梗概员" in c["messages"][0]["content"]
                       or "概览员" in c["messages"][0]["content"]]
            self.assertEqual(len(prepass), 0)

    def test_toggle_off(self):
        """关闭 book_understanding：不预扫，prompt 用「（无）」占位。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.book_understanding = False

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            self.assertFalse(store.load_chapter(0).meta.get("source_digest"))
            self.assertFalse((store.load_analysis() or {}).get("book_synopsis"))
            prepass = [c for c in client.calls
                       if "梗概员" in c["messages"][0]["content"]
                       or "概览员" in c["messages"][0]["content"]]
            self.assertEqual(len(prepass), 0)


class TestRunSteps(unittest.TestCase):
    def test_subset_only_assemble(self):
        """run_steps 步骤子集：仅回填时不应再产生翻译调用（幂等）。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run_steps(txt, {"translate"})
            # 仅回填，不应再翻译
            client2 = FakeClient(handler=routing_handler)
            res = Orchestrator(cfg, client=client2).run_steps(txt, {"assemble"})
            self.assertTrue(res["output"].endswith(".epub"))
            self.assertTrue(os.path.isfile(res["output"]))
            translate_calls = [c for c in client2.calls
                               if "文学翻译" in c["messages"][0]["content"]]
            self.assertEqual(len(translate_calls), 0)


class TestReviewReporting(unittest.TestCase):
    """章末审校 + 严重项自动重译（autofix_severe）。"""

    # 样例首段「第一章　出会い」7 字；fix 需在 3-21 字间（比值 0.3-3.0）方可通过长度校验
    FIX_TEXT = "第一章 邂逅"   # 7 字，比值 1.0

    def _handler(self, fix_text):
        """审校每块报 index 0 漏译；带【审校意见】的翻译调用返回定向重译文。"""
        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in sys:
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "漏了一句", "suggestion": "补上"}
                ]}, ensure_ascii=False)
            if "文学翻译" in sys and "【审校意见】" in user:
                return json.dumps({"translations": [fix_text]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)
        return handler

    def _run(self, d, *, autofix, fix_text=None):
        txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        cfg.pipeline.autofix_severe = autofix
        handler = self._handler(fix_text or self.FIX_TEXT)
        return Orchestrator(cfg, client=FakeClient(handler=handler)).run(txt)

    def test_autofix_adopts_retranslation(self):
        """autofix 开：严重项定向重译被采纳 → target 更新、fixed=True。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=True)
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is True for i in flagged))
            self.assertTrue(all(i.get("stage") == "review" for i in flagged))
            self.assertTrue(all("chapter" in i for i in flagged))
            self.assertEqual(ch.text_segments[0].target, self.FIX_TEXT)

            from trans_novel.glossary.store import GlossaryCheckpoint, GlossaryStore
            from trans_novel.pipeline.runstore import translation_fingerprint

            glossary = GlossaryStore(store.glossary_path)
            plan = ch.meta["glossary_plan"]
            for unit in plan["units"]:
                start = unit["start_index"]
                segments = ch.text_segments[start:start + unit["count"]]
                self.assertTrue(glossary.checkpoint_matches(GlossaryCheckpoint(
                    scope="batch",
                    chapter=0,
                    start_index=start,
                    count=len(segments),
                    fingerprint=translation_fingerprint(segments),
                )))
            chapter_plan = ch.meta["chapter_glossary_plan"]
            batches = Orchestrator._batch_glossary_checkpoints(
                0, ch.text_segments, plan["units"]
            )
            windows = Orchestrator._chapter_glossary_window_checkpoints(
                0, ch.text_segments, chapter_plan
            )
            self.assertTrue(glossary.chapter_completion_matches_v2(
                chapter=0,
                plan_fingerprint=chapter_plan["fingerprint"],
                batch_checkpoints=batches,
                window_checkpoints=windows,
            ))
            glossary.close()
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertIn(
                "glossary_post_review_refreshed",
                [event["event"] for event in events],
            )
            context = store.load_context()
            self.assertIn(self.FIX_TEXT, context["recent_targets"])
            self.assertEqual(
                context,
                Orchestrator._context_before_chapter(store, 2).to_dict(),
            )

    def test_post_review_success_clears_pre_review_glossary_failure(self):
        """Successful post-review reconciliation is authoritative in one run."""
        from trans_novel.glossary.extractor import GlossaryExtractionError

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.autofix_severe = True
            orchestrator = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler(self.FIX_TEXT)),
            )
            original = orchestrator.extractor.extract_and_store
            failed = False

            def fail_first_window(
                glossary,
                source_text,
                target_text,
                chapter,
                *,
                checkpoint=None,
            ):
                nonlocal failed
                if (
                    checkpoint is not None
                    and checkpoint.scope == "chapter_window"
                    and not failed
                ):
                    failed = True
                    raise GlossaryExtractionError("terms_missing")
                return original(
                    glossary,
                    source_text,
                    target_text,
                    chapter,
                    checkpoint=checkpoint,
                )

            with patch.object(
                orchestrator.extractor,
                "extract_and_store",
                side_effect=fail_first_window,
            ):
                store = orchestrator.run(txt, only_chapter=0)

            chapter = store.load_chapter(0)
            self.assertEqual(chapter.text_segments[0].target, self.FIX_TEXT)
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            event_names = [event["event"] for event in events]
            failure_index = next(
                index
                for index, event in enumerate(events)
                if event["event"] == "glossary_extraction_failed"
                and event.get("phase") == "chapter_window"
                and event.get("error_kind") == "terms_missing"
            )
            autofix_index = event_names.index("autofix_applied")
            derived_index = event_names.index("chapter_glossary_derived")
            self.assertLess(failure_index, autofix_index)
            self.assertLess(autofix_index, derived_index)
            refreshed = [
                event
                for event in events
                if event["event"] == "glossary_post_review_refreshed"
                and event.get("chapter") == 0
            ]
            self.assertTrue(refreshed)
            self.assertTrue(refreshed[-1]["completed"])

            units = chapter.meta["glossary_plan"]["units"]
            plan = chapter.meta["chapter_glossary_plan"]
            batches = Orchestrator._batch_glossary_checkpoints(
                0, chapter.text_segments, units
            )
            windows = Orchestrator._chapter_glossary_window_checkpoints(
                0, chapter.text_segments, plan
            )
            glossary = store.open_glossary()
            self.assertTrue(glossary.chapter_completion_matches_v2(
                chapter=0,
                plan_fingerprint=plan["fingerprint"],
                batch_checkpoints=batches,
                window_checkpoints=windows,
            ))
            glossary.close()
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )

    def test_backtranslation_uses_final_autofix_targets_and_chapter_indices(self):
        """Sampling happens after autofix and maps sample-local issue indices."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.autofix_severe = True
            cfg.pipeline.backtranslate_sample = 1.0
            base_handler = self._handler(self.FIX_TEXT)

            def handler(messages, tier, json_mode):
                if "翻译保真度核查员" in messages[0]["content"]:
                    return json.dumps(
                        {"issues": [{"index": 1, "detail": "sample issue"}]},
                        ensure_ascii=False,
                    )
                return base_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            store = Orchestrator(cfg, client=client).run(txt)
            backtranslation_calls = [
                call for call in client.calls
                if "回译译者" in call["messages"][0]["content"]
            ]
            self.assertTrue(backtranslation_calls)
            self.assertTrue(any(
                self.FIX_TEXT in call["messages"][-1]["content"]
                for call in backtranslation_calls
            ))
            issues = store.load_chapter(0).meta["backtranslation_issues"]
            self.assertTrue(issues)
            self.assertEqual(issues[0]["index"], 1)

    def test_resume_rebuilds_backtranslation_sample_from_saved_targets(self):
        """A crash after target save cannot suppress deterministic sampling."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review = False
            cfg.pipeline.polish = False
            cfg.pipeline.book_understanding = False
            cfg.pipeline.backtranslate_sample = 1.0
            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).prepare(txt)
            chapter = store.load_chapter(0)
            for index, segment in enumerate(chapter.text_segments):
                segment.target = f"中断前已存译文{index}"
            store.save_chapter(chapter)

            client = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=client).run(txt, only_chapter=0)
            self.assertEqual(_translated_para_count(client.calls), 0)
            self.assertTrue(any(
                "回译译者" in call["messages"][0]["content"]
                for call in client.calls
            ))

    def test_autofix_off_reports_only(self):
        """autofix 关：仅上报 fixed=False，正文不动。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=False)
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is False for i in flagged))
            self.assertNotEqual(ch.text_segments[0].target, self.FIX_TEXT)

    def test_autofix_rejects_short_retranslation(self):
        """重译结果过短（疑漏译）→ 不采纳，fixed=False，保留原译。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=True, fix_text="短")
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is False for i in flagged))
            self.assertNotEqual(ch.text_segments[0].target, "短")

    def test_review_index_mapping(self):
        """整章多块审校时，块内 index 正确映射回章内段号。"""
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "x", "suggestion": ""}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8   # 审校块预算=24 → 每段自成一块
            cfg.pipeline.autofix_severe = False
            store = Orchestrator(cfg, client=FakeClient(handler=handler)).run(txt)
            ch = store.load_chapter(0)
            idxs = sorted(i["index"] for i in ch.meta["review_issues"]
                          if i.get("type") == "missing")
            # 每块报 index 0 → 映射后应为各块首段的章内段号（0,1,2,...互不相同）
            self.assertEqual(idxs, list(range(len(ch.text_segments))))

    def test_review_rejects_boolean_index(self):
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return json.dumps({"issues": [{
                    "index": False,
                    "type": "missing",
                    "detail": "布尔值不是段号",
                }]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.autofix_severe = True
            client = FakeClient(handler=handler)
            store = Orchestrator(cfg, client=client).run(txt)
            self.assertFalse(any(
                issue.get("detail") == "布尔值不是段号"
                for issue in store.load_chapter(0).meta["review_issues"]
            ))

    def test_length_issues_survive_chapter_review(self):
        """批次廉价长度问题不能被章末 review 覆盖掉。"""
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps({"translations": ["" for _ in range(n)]},
                                  ensure_ascii=False)
            if "译文审校" in system:
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "漏译", "suggestion": "补译"}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.autofix_severe = False
            cfg.pipeline.book_understanding = False
            store = Orchestrator(cfg, client=FakeClient(handler=handler)).run(txt)
            issues = store.load_chapter(0).meta["review_issues"]
            self.assertTrue(any(i.get("stage") == "length" for i in issues))
            self.assertTrue(any(i.get("stage") == "review" for i in issues))


class TestStyleAnalysis(unittest.TestCase):
    def _long_doc(self, d):
        from trans_novel.ingest.segmenter import load_document
        txt = os.path.join(d, "long.txt")
        chapters = []
        for i in range(3):
            # 段落勿以「第N章」开头，避免被 TXT reader 的章标题启发式误判
            body = "\n\n".join(f"章{i}の段落{j}です。" + "あ" * 60 for j in range(8))
            chapters.append(f"# 第{i}章\n\n{body}")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n\n".join(chapters))
        return load_document(txt, "ja", "zh")

    def test_sample_text_multipoint(self):
        """labeled=True 多点采样带三个标注；labeled=False 为纯源文单段。"""
        with tempfile.TemporaryDirectory() as d:
            doc = self._long_doc(d)
            labeled = Orchestrator._sample_text(doc)
            for tag in ("【开头样章】", "【中部样章】", "【结尾样章】"):
                self.assertIn(tag, labeled)
            plain = Orchestrator._sample_text(doc, labeled=False)
            self.assertNotIn("样章】", plain)
            self.assertIn("章0の段落0です", plain)

    def test_sample_text_short_book_dedup(self):
        """单章书：三个采样点重合，只取一次、不重复。"""
        with tempfile.TemporaryDirectory() as d:
            from trans_novel.ingest.segmenter import load_document
            txt = os.path.join(d, "short.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("# 唯一章\n\n" + "长段落。" + "あ" * 300)
            doc = load_document(txt, "ja", "zh")
            sample = Orchestrator._sample_text(doc)
            self.assertEqual(sample.count("【开头样章】"), 1)
            self.assertNotIn("【中部样章】", sample)
            self.assertNotIn("【结尾样章】", sample)

    def test_sample_text_skips_front_matter(self):
        from trans_novel.ingest.models import Chapter, Document, Segment

        copyright_text = "Copyright © 2015. All rights reserved. ISBN: 1." + "x" * 260
        chapter_text = "Financial contracts define obligations and payoffs. " + "y" * 260
        doc = Document(
            title="Book",
            source_lang="en",
            target_lang="zh",
            fmt="epub",
            chapters=[
                Chapter(
                    index=0,
                    title="Copyright Page",
                    segments=[Segment(index=0, source=copyright_text)],
                ),
                Chapter(
                    index=1,
                    title="1 Financial Contracts",
                    segments=[Segment(index=0, source=chapter_text)],
                ),
            ],
        )

        sample = Orchestrator._sample_text(doc)
        self.assertIn("Financial contracts", sample)
        self.assertNotIn("Copyright ©", sample)

    def test_style_brief_new_fields(self):
        """style_brief 渲染新风格维度；旧 analysis（缺新字段）不报错不输出。"""
        from trans_novel.agents.analyzer import Analyzer
        from trans_novel.llm.base import FakeClient as FC

        cfg = _config("state")
        ana = Analyzer(FC(), cfg)
        brief = ana.style_brief({
            "genre": "校园", "pacing": "短句为主", "register": "口语",
            "dialogue_style": "语气词丰富", "narration": "第一人称",
        })
        self.assertIn("句式节奏：短句为主", brief)
        self.assertIn("语域：口语", brief)
        self.assertIn("对话风格：语气词丰富", brief)
        self.assertIn("叙事：第一人称", brief)
        # 旧格式：只有老字段
        old = ana.style_brief({"genre": "校园", "tone": "冷峻"})
        self.assertIn("体裁：校园", old)
        self.assertNotIn("句式节奏", old)


class TestGlossaryScope(unittest.TestCase):
    def _run_with_terms(self, d, scope):
        from trans_novel.glossary.store import GlossaryStore, GlossaryTerm

        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        cfg.pipeline.glossary_scope = scope

        orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
        store = orch.prepare(txt)
        g = GlossaryStore(store.glossary_path)
        # ①锁定人物（source 不在正文）②无关术语（source/alias 均不在正文）③alias 在正文出现
        g.upsert_term(GlossaryTerm(source="外部人物X", target="外部译名",
                                   type="人物", locked=True))
        g.upsert_term(GlossaryTerm(source="無関係用語", target="无关术语", type="术语"))
        g.upsert_term(GlossaryTerm(source="ホリキタ", target="堀北译名",
                                   aliases=["堀北"], type="术语"))
        g.close()

        client = FakeClient(handler=routing_handler)
        Orchestrator(cfg, client=client).run(txt)
        return ["\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "文学翻译" in c["messages"][0]["content"]]

    def test_chapter_scope_prunes(self):
        """chapter：锁定人物保留、无关术语剔除、alias 命中保留。"""
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "chapter")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertIn("外部人物X", p)     # 锁定人物：始终保留
                self.assertNotIn("無関係用語", p)  # 本章未出现：剔除
                self.assertIn("ホリキタ", p)      # 别名「堀北」在正文：保留

    def test_full_scope_keeps_all(self):
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "full")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertIn("外部人物X", p)
                self.assertIn("無関係用語", p)
                self.assertIn("ホリキタ", p)

    def test_batch_glossary_refreshes_following_prompts(self):
        """批次翻译后实时抽取术语，后续批次 prompt 立即带上新称谓。"""
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps({"translations": ["小夏帆" for _ in range(n)]},
                                  ensure_ascii=False)
            if "术语" in system and "抽取器" in system and "夏帆ちゃん" in user and "小夏帆" in user:
                return json.dumps({"terms": [
                    {"source": "夏帆ちゃん", "target": "小夏帆",
                     "type": "称谓", "aliases": ["夏帆"], "note": "亲昵称呼"}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n"
                    "「夏帆ちゃん」と母親が言った。\n\n"
                    "夏帆ちゃんは窓の外を見た。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.consistency_qa = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 10

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(txt)

            translate_prompts = [
                "\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "文学翻译" in c["messages"][0]["content"]
            ]
            self.assertGreaterEqual(len(translate_prompts), 3)
            self.assertIn("夏帆ちゃん → 小夏帆", translate_prompts[-1])

    def test_chapter_glossary_refreshes_review_prompt(self):
        """全章兜底术语抽取在 review 前执行，章末审校能看到新称谓。"""
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps({"translations": ["小夏帆" for _ in range(n)]},
                                  ensure_ascii=False)
            if "术语" in system and "抽取器" in system and "夏帆ちゃん" in user:
                return json.dumps({"terms": [
                    {"source": "夏帆ちゃん", "target": "小夏帆",
                     "type": "称谓", "aliases": ["夏帆"], "note": "亲昵称呼"}
                ]}, ensure_ascii=False)
            if "译文审校" in system:
                self.assertIn("夏帆ちゃん → 小夏帆", user)
                return json.dumps({"issues": []}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("# 第一章\n\n「夏帆ちゃん」と母親が言った。\n")
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.consistency_qa = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 200

            Orchestrator(cfg, client=FakeClient(handler=handler)).run(txt)


class TestGlossaryCheckpointResume(unittest.TestCase):
    """翻译产物与术语抽取解耦后的断点恢复契约。"""

    @staticmethod
    def _write_book(root: str) -> str:
        path = os.path.join(root, "checkpoint.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# 第一章\n\n"
                "夏帆ちゃんは窓の外を見た。\n\n"
                "母親は夏帆ちゃんに静かに声をかけた。\n\n"
                "二人はしばらく黙っていた。\n"
            )
        return path

    @staticmethod
    def _write_windowed_book(root: str) -> str:
        """Seven paragraphs become seven canonical units at a one-char budget."""
        path = os.path.join(root, "windowed.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(
                f"夏帆ちゃんは第{index}地点を調査した。"
                for index in range(7)
            ))
            f.write("\n")
        return path

    @staticmethod
    def _checkpoint_config(root: str, *, batch_chars: int = 10_000):
        cfg = _config(os.path.join(root, "state"))
        cfg.segment.max_chars_per_batch = batch_chars
        cfg.pipeline.polish = False
        cfg.pipeline.review = False
        cfg.pipeline.consistency_qa = False
        cfg.pipeline.book_understanding = False
        return cfg

    @staticmethod
    def _handler(*, glossary_reply: str | None = None):
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps(
                    {"translations": [f"续跑译文{i}" for i in range(n)]},
                    ensure_ascii=False,
                )
            if "术语" in system and "抽取器" in system:
                if glossary_reply is not None:
                    return glossary_reply
                return json.dumps({"terms": [{
                    "source": "夏帆ちゃん",
                    "target": "小夏帆",
                    "type": "称谓",
                }]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)
        return handler

    @staticmethod
    def _events(store: RunStore) -> list[dict]:
        if not os.path.isfile(store.event_log_path):
            return []
        with open(store.event_log_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def _glossary_call_count(client: FakeClient) -> int:
        return sum(
            "术语" in c["messages"][0]["content"]
            and "抽取器" in c["messages"][0]["content"]
            for c in client.calls
        )

    def _completed_windowed_run(self, root: str):
        txt = self._write_windowed_book(root)
        cfg = self._checkpoint_config(root, batch_chars=1)
        store = Orchestrator(
            cfg,
            client=FakeClient(handler=self._handler()),
        ).run(txt)
        chapter = store.load_chapter(0)
        units = chapter.meta["glossary_plan"]["units"]
        plan = chapter.meta["chapter_glossary_plan"]
        self.assertEqual(len(units), 7)
        self.assertGreaterEqual(len(plan["windows"]), 3)
        return txt, cfg, store, chapter, units, plan

    def test_failed_window_resume_retries_only_that_window_then_is_idempotent(self):
        """One v2 window protocol failure leaves recoverable, exact progress."""
        from trans_novel.glossary.extractor import (
            GlossaryExtractionError,
        )

        with tempfile.TemporaryDirectory() as d:
            txt = self._write_windowed_book(d)
            cfg = self._checkpoint_config(d, batch_chars=1)
            orchestrator = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            )
            original = orchestrator.extractor.extract_and_store
            failed = False

            def fail_first_window(
                glossary,
                source_text,
                target_text,
                chapter,
                *,
                checkpoint=None,
            ):
                nonlocal failed
                if (
                    checkpoint is not None
                    and checkpoint.scope == "chapter_window"
                    and not failed
                ):
                    failed = True
                    raise GlossaryExtractionError("terms_missing")
                return original(
                    glossary,
                    source_text,
                    target_text,
                    chapter,
                    checkpoint=checkpoint,
                )

            with patch.object(
                orchestrator.extractor,
                "extract_and_store",
                side_effect=fail_first_window,
            ):
                store = orchestrator.run(txt)

            chapter = store.load_chapter(0)
            plan = chapter.meta["chapter_glossary_plan"]
            self.assertEqual(len(chapter.meta["glossary_plan"]["units"]), 7)
            failures = [
                event
                for event in self._events(store)
                if event["event"] == "glossary_extraction_failed"
                and event.get("phase") == "chapter_window"
            ]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["error_kind"], "terms_missing")
            manifest = store.load_manifest()
            self.assertEqual(manifest["chapters"][0]["status"], STATUS_DONE)
            self.assertEqual(
                manifest["chapters"][0]["glossary_status"],
                STATUS_PENDING,
            )
            self.assertEqual(manifest["titles_status"], STATUS_PENDING)
            self.assertNotIn("title_translated", manifest["chapters"][0])

            glossary = store.open_glossary()
            parent_count = glossary.conn.execute(
                "SELECT COUNT(*) FROM glossary_extraction_checkpoints "
                "WHERE scope='chapter' AND chapter=0"
            ).fetchone()[0]
            window_count = glossary.conn.execute(
                "SELECT COUNT(*) FROM glossary_chapter_window_checkpoints "
                "WHERE chapter=0 AND plan_fingerprint=?",
                (plan["fingerprint"],),
            ).fetchone()[0]
            glossary.close()
            self.assertEqual(parent_count, 0)
            self.assertEqual(window_count, len(plan["windows"]) - 1)

            targets_before = [
                segment.target for segment in chapter.text_segments
            ]
            context_before = store.load_context()
            event_offset = len(self._events(store))
            resumed = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=resumed).run(txt)

            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 1)
            delta = self._events(store)[event_offset:]
            extracted = [
                event
                for event in delta
                if event["event"] == "chapter_glossary_window_extracted"
            ]
            self.assertEqual(len(extracted), 1)
            self.assertEqual(
                [segment.target for segment in store.load_chapter(0).text_segments],
                targets_before,
            )
            self.assertEqual(store.load_context(), context_before)
            manifest = store.load_manifest()
            self.assertEqual(
                manifest["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )
            self.assertEqual(manifest["titles_status"], STATUS_DONE)
            self.assertTrue(manifest["chapters"][0].get("title_translated"))

            idempotent = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=idempotent).run(txt)
            self.assertEqual(idempotent.calls, [])

    def test_model_failure_keeps_translation_done_and_retries_only_glossary(self):
        """模型协议失败不丢译文；续跑只补术语，不再次调用译者。"""
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)

            failed = FakeClient(handler=self._handler(glossary_reply="not valid json"))
            store = Orchestrator(cfg, client=failed).run(txt)
            manifest = store.load_manifest()
            self.assertEqual(manifest["chapters"][0]["status"], STATUS_DONE)
            self.assertEqual(manifest["chapters"][0]["glossary_status"], STATUS_PENDING)
            self.assertEqual(manifest["titles_status"], STATUS_PENDING)
            self.assertNotIn("title_translated", manifest["chapters"][0])
            before = [s.target for s in store.load_chapter(0).text_segments]
            context_before = store.load_context()
            self.assertTrue(all(before))
            failures = [
                e for e in self._events(store)
                if e["event"] == "glossary_extraction_failed"
            ]
            self.assertTrue(failures)
            self.assertTrue(all(e.get("error_kind") for e in failures))

            resumed = FakeClient(handler=self._handler())
            store = Orchestrator(cfg, client=resumed).run(txt)
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertGreater(self._glossary_call_count(resumed), 0)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )
            self.assertEqual(store.load_manifest()["titles_status"], STATUS_DONE)
            self.assertTrue(
                store.load_manifest()["chapters"][0].get("title_translated")
            )
            self.assertEqual(
                [s.target for s in store.load_chapter(0).text_segments],
                before,
            )
            self.assertEqual(store.load_context(), context_before)
            completed = [
                e for e in self._events(store)
                if e["event"] in {
                    "batch_glossary_extracted",
                    "chapter_glossary_extracted",
                }
                and e.get("checkpoint_version") == 1
            ]
            self.assertTrue(completed)
            self.assertTrue(all(e.get("completed") is True for e in completed))
            self.assertTrue(all(e.get("fingerprint") for e in completed))
            self.assertTrue(all(e.get("generation_id") for e in completed))

    def test_persistence_failure_is_fatal_after_targets_are_saved(self):
        """SQLite 写失败必须中止，但刚完成的 canonical unit 译文已经原子落盘。"""
        from trans_novel.glossary.extractor import GlossaryPersistenceError
        from trans_novel.glossary.store import GlossaryStore

        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            store = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            ).prepare(txt)

            with patch.object(
                GlossaryStore,
                "upsert_terms",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ):
                with self.assertRaises(GlossaryPersistenceError):
                    Orchestrator(
                        cfg,
                        client=FakeClient(handler=self._handler()),
                    ).run(txt)

            self.assertTrue(all(s.target for s in store.load_chapter(0).text_segments))
            manifest = store.load_manifest()
            self.assertEqual(manifest["chapters"][0]["status"], STATUS_PENDING)
            self.assertEqual(manifest["chapters"][0]["glossary_status"], STATUS_PENDING)
            self.assertIn(
                "glossary_persistence_failed",
                [e["event"] for e in self._events(store)],
            )

    def test_partial_canonical_unit_translates_only_missing_segments(self):
        """unit 内部分段已保存时，只翻连续缺口，原 target 必须逐字保持。"""
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            store = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            ).prepare(txt)
            chapter = store.load_chapter(0)
            kept = "  已保存译文\n标点，空白都不能变化。  "
            chapter.text_segments[1].target = kept
            store.save_chapter(chapter)

            client = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=client).run(txt)

            self.assertEqual(
                _translated_para_count(client.calls),
                len(chapter.text_segments) - 1,
            )
            self.assertEqual(store.load_chapter(0).text_segments[1].target, kept)

    def test_persisted_glossary_plan_ignores_later_batch_budget_change(self):
        """canonical plan 一经持久化，续跑配置变化不得重新切 unit。"""
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d, batch_chars=25)
            bad_reply = "not valid json"
            store = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler(glossary_reply=bad_reply)),
            ).run(txt)
            before = store.load_chapter(0).meta["glossary_plan"]
            self.assertEqual(before["version"], 1)
            self.assertGreater(len(before["units"]), 1)
            self.assertTrue(all(
                {"start_index", "count", "source_fingerprint"} <= set(unit)
                for unit in before["units"]
            ))

            changed = self._checkpoint_config(d, batch_chars=10_000)
            Orchestrator(
                changed,
                client=FakeClient(handler=self._handler(glossary_reply=bad_reply)),
            ).run(txt)
            self.assertEqual(
                store.load_chapter(0).meta["glossary_plan"],
                before,
            )

    def test_db_checkpoint_is_authoritative_when_audit_event_is_missing(self):
        """事务已提交但 event 未追加的崩溃窗，续跑从 DB 跳过模型抽取。"""
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            store = Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            ).run(txt)

            success_events = {
                "batch_glossary_extracted",
                "chapter_glossary_extracted",
            }
            remaining = [
                event for event in self._events(store)
                if event["event"] not in success_events
            ]
            with open(store.event_log_path, "w", encoding="utf-8") as f:
                for event in remaining:
                    f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            manifest = store.load_manifest()
            manifest["chapters"][0]["glossary_status"] = STATUS_PENDING
            store.save_manifest(manifest)

            resumed = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=resumed).run(txt)

            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 0)
            names = [e["event"] for e in self._events(store)]
            self.assertIn("batch_glossary_skipped", names)
            self.assertIn("chapter_glossary_skipped", names)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )

    def test_done_manifest_is_reopened_when_chapter_checkpoint_is_missing(self):
        """Missing v2 parent is re-derived from retained exact children."""
        with tempfile.TemporaryDirectory() as d:
            txt, cfg, store, chapter, units, plan = (
                self._completed_windowed_run(d)
            )
            batches = Orchestrator._batch_glossary_checkpoints(
                0, chapter.text_segments, units
            )
            windows = Orchestrator._chapter_glossary_window_checkpoints(
                0, chapter.text_segments, plan
            )
            glossary = store.open_glossary()
            glossary.conn.execute(
                "DELETE FROM glossary_extraction_checkpoints "
                "WHERE scope='chapter' AND chapter=0 AND version=2"
            )
            glossary.conn.commit()
            self.assertTrue(all(
                glossary.checkpoint_matches(checkpoint)
                for checkpoint in batches + windows
            ))
            glossary.close()

            prepare_client = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=prepare_client).prepare(txt)
            self.assertEqual(prepare_client.calls, [])
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_PENDING,
            )

            resumed = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=resumed).run(txt)
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 0)
            self.assertIn(
                "chapter_glossary_derived",
                [event["event"] for event in self._events(store)],
            )
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )

            glossary = store.open_glossary()
            self.assertTrue(glossary.chapter_completion_matches_v2(
                chapter=0,
                plan_fingerprint=plan["fingerprint"],
                batch_checkpoints=batches,
                window_checkpoints=windows,
            ))
            glossary.close()

    def test_missing_window_child_reopens_and_retries_only_that_window(self):
        """A retained parent cannot hide one missing v2 window child."""
        with tempfile.TemporaryDirectory() as d:
            txt, cfg, store, chapter, units, plan = (
                self._completed_windowed_run(d)
            )
            windows = Orchestrator._chapter_glossary_window_checkpoints(
                0, chapter.text_segments, plan
            )
            missing = windows[1]
            glossary = store.open_glossary()
            parent_count = glossary.conn.execute(
                "SELECT COUNT(*) FROM glossary_extraction_checkpoints "
                "WHERE scope='chapter' AND chapter=0 AND version=2"
            ).fetchone()[0]
            glossary.conn.execute(
                "DELETE FROM glossary_chapter_window_checkpoints "
                "WHERE chapter=0 AND start_index=?",
                (missing.start_index,),
            )
            glossary.conn.commit()
            glossary.close()
            self.assertEqual(parent_count, 1)

            Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            ).prepare(txt)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_PENDING,
            )

            event_offset = len(self._events(store))
            resumed = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=resumed).run(txt)
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 1)
            delta = self._events(store)[event_offset:]
            extracted = [
                event
                for event in delta
                if event["event"] == "chapter_glossary_window_extracted"
            ]
            self.assertEqual(
                [(event["start_index"], event["count"]) for event in extracted],
                [(missing.start_index, missing.count)],
            )
            skipped = [
                event
                for event in delta
                if event["event"] == "chapter_glossary_window_skipped"
            ]
            self.assertEqual(len(skipped), len(windows) - 1)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )

    def test_changed_overlap_unit_refreshes_exact_batch_and_windows(self):
        """Target drift invalidates one unit and only its two overlapping windows."""
        with tempfile.TemporaryDirectory() as d:
            txt, cfg, store, chapter, units, plan = (
                self._completed_windowed_run(d)
            )
            changed_unit = units[2]
            changed_index = changed_unit["start_index"]
            affected_windows = [
                window
                for window in plan["windows"]
                if window["start_index"]
                <= changed_index
                < window["start_index"] + window["count"]
            ]
            unaffected_windows = [
                window
                for window in plan["windows"]
                if window not in affected_windows
            ]
            self.assertEqual(len(affected_windows), 2)
            self.assertTrue(unaffected_windows)

            chapter.text_segments[changed_index].target += "（人工修订）"
            store.save_chapter(chapter)
            Orchestrator(
                cfg,
                client=FakeClient(handler=self._handler()),
            ).prepare(txt)
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_PENDING,
            )

            event_offset = len(self._events(store))
            resumed = FakeClient(handler=self._handler())
            Orchestrator(cfg, client=resumed).run(txt)
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 3)
            delta = self._events(store)[event_offset:]
            batches = [
                event
                for event in delta
                if event["event"] == "batch_glossary_extracted"
            ]
            self.assertEqual(
                [(event["start_index"], event["count"]) for event in batches],
                [(changed_unit["start_index"], changed_unit["count"])],
            )
            extracted_windows = [
                event
                for event in delta
                if event["event"] == "chapter_glossary_window_extracted"
            ]
            self.assertEqual(
                {
                    (event["start_index"], event["count"])
                    for event in extracted_windows
                },
                {
                    (window["start_index"], window["count"])
                    for window in affected_windows
                },
            )
            skipped_windows = [
                event
                for event in delta
                if event["event"] == "chapter_glossary_window_skipped"
            ]
            self.assertEqual(
                {
                    (event["start_index"], event["count"])
                    for event in skipped_windows
                },
                {
                    (window["start_index"], window["count"])
                    for window in unaffected_windows
                },
            )
            self.assertEqual(
                store.load_manifest()["chapters"][0]["glossary_status"],
                STATUS_DONE,
            )

    def test_new_format_done_chapter_with_empty_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            store = Orchestrator(
                cfg, client=FakeClient(handler=self._handler())
            ).run(txt)
            chapter = store.load_chapter(0)
            chapter.text_segments[0].target = ""
            store.save_chapter(chapter)

            with self.assertRaisesRegex(RuntimeError, "新格式状态"):
                Orchestrator(
                    cfg, client=FakeClient(handler=self._handler())
                ).prepare(txt)

    def test_one_marker_glossary_state_contradictions_fail_closed(self):
        for defect in ("missing_status", "done_without_plan"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                txt = self._write_book(d)
                cfg = self._checkpoint_config(d)
                store = Orchestrator(
                    cfg, client=FakeClient(handler=self._handler())
                ).run(txt)
                chapter = store.load_chapter(0)
                manifest = store.load_manifest()
                if defect == "missing_status":
                    manifest["chapters"][0].pop("glossary_status")
                else:
                    chapter.meta.pop("glossary_plan")
                    store.save_chapter(chapter)
                store.save_manifest(manifest)

                with self.assertRaisesRegex(RuntimeError, "术语.*(缺失|缺少)"):
                    Orchestrator(
                        cfg, client=FakeClient(handler=self._handler())
                    ).prepare(txt)

    def test_new_format_both_chapter_markers_missing_fails_closed(self):
        for with_empty_target in (False, True):
            with self.subTest(with_empty_target=with_empty_target), tempfile.TemporaryDirectory() as d:
                txt = self._write_book(d)
                cfg = self._checkpoint_config(d)
                store = Orchestrator(
                    cfg, client=FakeClient(handler=self._handler())
                ).run(txt)
                chapter = store.load_chapter(0)
                chapter.meta.pop("glossary_plan")
                if with_empty_target:
                    chapter.text_segments[0].target = ""
                store.save_chapter(chapter)
                manifest = store.load_manifest()
                manifest["chapters"][0].pop("glossary_status")
                manifest["chapters"][0].pop("glossary_legacy", None)
                store.save_manifest(manifest)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "(没有 legacy 迁移证据|新格式状态)",
                ):
                    Orchestrator(
                        cfg, client=FakeClient(handler=self._handler())
                    ).prepare(txt)

    def test_legacy_marker_cannot_coexist_with_new_chapter_state(self):
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            store = Orchestrator(
                cfg, client=FakeClient(handler=self._handler())
            ).run(txt)
            manifest = store.load_manifest()
            manifest["chapters"][0]["glossary_legacy"] = True
            store.save_manifest(manifest)

            with self.assertRaisesRegex(RuntimeError, "legacy 迁移标记"):
                Orchestrator(
                    cfg, client=FakeClient(handler=self._handler())
                ).prepare(txt)

    def test_null_glossary_plan_fails_closed_in_all_resume_states(self):
        for state in ("pending", "done", "legacy_hole"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                txt = self._write_book(d)
                cfg = self._checkpoint_config(d)
                if state == "pending":
                    store = Orchestrator(
                        cfg, client=FakeClient(handler=self._handler())
                    ).prepare(txt)
                else:
                    store = Orchestrator(
                        cfg, client=FakeClient(handler=self._handler())
                    ).run(txt)
                chapter = store.load_chapter(0)
                chapter.meta["glossary_plan"] = None
                if state == "legacy_hole":
                    chapter.text_segments[0].target = ""
                store.save_chapter(chapter)
                if state == "legacy_hole":
                    manifest = store.load_manifest()
                    entry = manifest["chapters"][0]
                    entry.pop("glossary_status")
                    entry["glossary_legacy"] = True
                    store.save_manifest(manifest)

                with self.assertRaisesRegex(RuntimeError, "(术语计划|新格式状态)"):
                    Orchestrator(
                        cfg, client=FakeClient(handler=self._handler())
                    ).prepare(txt)

    def test_empty_done_chapter_without_plan_is_valid(self):
        from trans_novel.glossary.store import GlossaryStore
        from trans_novel.ingest.models import Chapter, Document

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "empty-run"))
            glossary = GlossaryStore(store.glossary_path)
            document = Document(
                title="Empty",
                source_lang="ja",
                target_lang="zh",
                fmt="txt",
                chapters=[Chapter(index=0, title="Empty", segments=[])],
            )
            store.init_from_document(
                document,
                glossary_generation_id=glossary.generation_id,
            )
            store.set_chapter_status(0, STATUS_DONE)
            store.set_chapter_glossary_status(0, STATUS_DONE)
            Orchestrator(
                self._checkpoint_config(d),
                client=FakeClient(handler=self._handler()),
            )._reconcile_glossary_statuses(store, glossary)
            glossary.close()

    def test_title_retry_marker_survives_glossary_recovery_failure(self):
        """A title protocol failure remains pending after glossary recovery."""
        with tempfile.TemporaryDirectory() as d:
            txt = self._write_book(d)
            cfg = self._checkpoint_config(d)
            Orchestrator(
                cfg,
                client=FakeClient(
                    handler=self._handler(glossary_reply="not valid json")
                ),
            ).run(txt)

            base = self._handler()

            def bad_title(messages, tier, json_mode):
                if "标题翻译专家" in messages[0]["content"]:
                    return json.dumps({"titles": []}, ensure_ascii=False)
                return base(messages, tier, json_mode)

            store = Orchestrator(
                cfg, client=FakeClient(handler=bad_title)
            ).run(txt)
            self.assertEqual(store.load_manifest()["titles_status"], STATUS_PENDING)

            client = FakeClient(handler=self._handler())
            store = Orchestrator(cfg, client=client).run(txt)
            self.assertEqual(store.load_manifest()["titles_status"], STATUS_DONE)
            self.assertTrue(any(
                "标题翻译专家" in call["messages"][0]["content"]
                for call in client.calls
            ))

    def test_invalid_persisted_plan_fails_closed_without_touching_targets(self):
        """plan 缺口、重叠或源指纹漂移时停止续跑，绝不猜测边界或改译文。"""
        from trans_novel.pipeline.runstore import source_fingerprint

        for defect in ("gap", "overlap", "source_fingerprint"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                txt = self._write_book(d)
                cfg = self._checkpoint_config(d)
                store = Orchestrator(
                    cfg,
                    client=FakeClient(handler=self._handler()),
                ).prepare(txt)
                chapter = store.load_chapter(0)
                chapter.text_segments[1].target = "  原样保留\n不可改写。  "
                sources = [segment.source for segment in chapter.text_segments]
                count = len(sources)

                def unit(start: int, size: int) -> dict:
                    return {
                        "start_index": start,
                        "count": size,
                        "source_fingerprint": source_fingerprint(
                            sources[start:start + size]
                        ),
                    }

                if defect == "gap":
                    units = [unit(1, count - 1)]
                elif defect == "overlap":
                    units = [unit(0, 2), unit(1, count - 1)]
                else:
                    units = [unit(0, count)]
                    units[0]["source_fingerprint"] = "0" * 64
                chapter.meta["glossary_plan"] = {"version": 1, "units": units}
                store.save_chapter(chapter)
                before = [s.target for s in chapter.text_segments]

                with self.assertRaisesRegex(RuntimeError, "术语计划"):
                    Orchestrator(
                        cfg,
                        client=FakeClient(handler=self._handler()),
                    ).run(txt)

                after = [s.target for s in store.load_chapter(0).text_segments]
                self.assertEqual(after, before)
                self.assertIn(
                    "glossary_plan_invalid",
                    [event["event"] for event in self._events(store)],
                )

    def _resume_with_legacy_batch_summary(self, root: str, summary: dict[str, int]):
        txt = self._write_book(root)
        cfg = self._checkpoint_config(root)
        store = Orchestrator(
            cfg,
            client=FakeClient(handler=self._handler(glossary_reply="not valid json")),
        ).run(txt)
        unit = store.load_chapter(0).meta["glossary_plan"]["units"][0]
        store.log_event(
            "batch_glossary_extracted",
            chapter=0,
            start_index=unit["start_index"],
            count=unit["count"],
            summary=summary,
        )

        resumed = FakeClient(handler=self._handler())
        Orchestrator(cfg, client=resumed).run(txt)
        return store, resumed

    def test_legacy_zero_summary_is_retried(self):
        """旧版全零 summary 不证明模型成功，batch 与 chapter 都必须重抽。"""
        with tempfile.TemporaryDirectory() as d:
            store, resumed = self._resume_with_legacy_batch_summary(d, {
                "inserted": 0,
                "updated": 0,
                "conflict": 0,
                "unchanged": 0,
            })
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 2)
            extracted = [
                e for e in self._events(store)
                if e["event"] == "batch_glossary_extracted"
                and e.get("checkpoint_version") == 1
            ]
            self.assertTrue(extracted)

    def test_legacy_nonzero_exact_event_promotes_batch_checkpoint(self):
        """旧版非零 summary + 同 key 精确 batch_translated 可晋升，batch 不重抽。"""
        with tempfile.TemporaryDirectory() as d:
            store, resumed = self._resume_with_legacy_batch_summary(d, {
                "inserted": 1,
                "updated": 0,
                "conflict": 0,
                "unchanged": 0,
            })
            self.assertEqual(_translated_para_count(resumed.calls), 0)
            self.assertEqual(self._glossary_call_count(resumed), 1)
            skipped = [
                e for e in self._events(store)
                if e["event"] == "batch_glossary_skipped"
            ]
            self.assertTrue(skipped)


class TestTierRouting(unittest.TestCase):
    def test_task_tiers(self):
        """机械任务走 fast 档、判断类走 cheap、翻译走 strong；梗概带 max_tokens 上限。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.backtranslate_sample = 1.0  # 强制触发回译

            client = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=client).run(txt)

            expect = {
                "章节梗概员": "fast", "全书概览员": "fast",
                "术语与称呼抽取器": "fast", "回译译者": "fast",
                "译文审校": "cheap", "保真度": "cheap",
                "文学翻译": "strong",
            }
            seen = set()
            for c in client.calls:
                system = c["messages"][0]["content"]
                for marker, tier in expect.items():
                    if marker in system:
                        self.assertEqual(c["tier"], tier, f"{marker} 应走 {tier} 档")
                        seen.add(marker)
                        if marker == "章节梗概员":
                            self.assertEqual(c["max_tokens"], 600)
                        if marker == "全书概览员":
                            self.assertEqual(c["max_tokens"], 1200)
            self.assertEqual(seen, set(expect), "各类调用都应出现")


class TestLangNormalize(unittest.TestCase):
    def test_normalize_lang(self):
        self.assertEqual(_normalize_lang("Japanese"), "ja")
        self.assertEqual(_normalize_lang("日语"), "ja")
        self.assertEqual(_normalize_lang("RU"), "ru")
        self.assertEqual(_normalize_lang("russian"), "ru")
        self.assertEqual(_normalize_lang("fr"), "fr")
        self.assertEqual(_normalize_lang("unknown"), "")
        self.assertEqual(_normalize_lang(""), "")


if __name__ == "__main__":
    unittest.main()
