"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.assemble.writer import assemble
from trans_novel.assemble.report import build_report
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.segmenter import load_document
from tests.sample_data import write_sample_txt, write_sample_epub
from tests.fake_llm import routing_handler


def _write_vertical_epub(path: str) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>縦書き小説</dc:title>
    <dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine page-progression-direction="rtl">
    <itemref idref="ch1"/>
  </spine>
</package>
"""
    ch1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" class="vrtl"><head>
<title>第一章</title><link rel="stylesheet" href="style.css"/>
</head><body>
<h1>第一章　出会い</h1>
<p>綾小路は教室の窓際に座っていた。</p>
</body></html>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/style.css", "html { writing-mode: vertical-rl; }")
        zf.writestr("OEBPS/ch1.xhtml", ch1)


def _config(state_dir: str):
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        "pipeline": {"review": True, "polish": True, "backtranslate_sample": 0.0},
        "paths": {"state_dir": state_dir},
    })


def _run(input_path, state_dir):
    cfg = _config(state_dir)
    orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
    return orch.run(input_path), cfg


class TestAssembleText(unittest.TestCase):
    def test_txt_input_to_txt(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="txt")
            self.assertTrue(out.endswith(".txt"))
            self.assertEqual(os.path.basename(out), "novel.zh.txt")
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("润0", content)  # 译文已写入

    def test_txt_input_to_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="epub")
            self.assertTrue(out.endswith(".epub"))
            self.assertEqual(os.path.basename(out), "novel.zh.epub")
            self.assertTrue(zipfile.is_zipfile(out))
            # 重新解析生成的 EPUB，应能读出章节且含译文
            doc = load_document(out, "ja", "zh")
            self.assertGreaterEqual(len(doc.chapters), 2)
            alltext = "".join(s.source for c in doc.chapters for s in c.text_segments)
            self.assertIn("润", alltext)


class TestAssembleEpub(unittest.TestCase):
    def test_epub_template_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("润0", html)            # 译文已替换
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertNotIn("綾小路は教室", html)  # 原文已被替换

    def test_vertical_epub_is_exported_as_horizontal_chinese(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "vertical.epub")
            _write_vertical_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertIn('page-progression-direction="ltr"', opf)
            self.assertIn("writing-mode: horizontal-tb", html)
            self.assertIn('lang="zh-Hans"', html)
            self.assertNotIn('class="vrtl"', html)


class TestTitleTranslation(unittest.TestCase):
    def test_manifest_keeps_book_title_and_translates_chapter_titles(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            # 书名不翻译；章节标题译出并写回 manifest（fake：标题0/1）
            m = store.load_manifest()
            self.assertNotIn("title_translated", m)
            self.assertTrue(all(c.get("title_translated") for c in m["chapters"]))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("サンプル小説", opf)       # OPF 书名保持原文
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertEqual(os.path.basename(out), "novel.zh.epub")

    def test_rewrite_targets_propagates_to_titles(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            store, cfg = _run(txt, os.path.join(d, "state"))
            # 手动写入含变体的标题译名
            m = store.load_manifest()
            m["title_translated"] = "佳穂传"
            m["chapters"][0]["title_translated"] = "佳穂登场"
            store.save_manifest(m)
            g = GlossaryStore(store.glossary_path)
            GlossaryAuditor._rewrite_targets(store, g, {"佳穂": "佳穗"})
            g.close()
            m2 = store.load_manifest()
            self.assertNotIn("title_translated", m2)                    # 书名译名字段被清理
            self.assertEqual(m2["chapters"][0]["title_translated"], "佳穗登场")  # 仅标题替换可直接保留

    def test_rewrite_nav_and_ncx_labels(self):
        from trans_novel.assemble.writer import _rewrite_toc

        nav = (b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
               b'<nav epub:type="toc"><ol>'
               b'<li><a href="ch1.xhtml">\xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0</a></li>'
               b'</ol></nav></body></html>')
        out = _rewrite_toc(nav, {"ch1.xhtml": "第一章译名"}, is_ncx=False)
        self.assertIn("第一章译名", out.decode("utf-8"))

        ncx = (b'<?xml version="1.0"?><ncx><navMap><navPoint>'
               b'<navLabel><text>old</text></navLabel>'
               b'<content src="text/ch1.xhtml#x"/></navPoint></navMap></ncx>')
        out2 = _rewrite_toc(ncx, {"ch1.xhtml": "第一章译名"}, is_ncx=True)
        dec = out2.decode("utf-8")
        self.assertIn("第一章译名", dec)
        self.assertNotIn(">old<", dec)

    def test_non_string_title_items_fall_back_to_source_titles(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "标题翻译专家" in messages[0]["content"]:
                    return json.dumps(
                        {"titles": [None, {"bad": True}]},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            store = Orchestrator(
                cfg, client=FakeClient(handler=handler)
            ).run(txt)
            manifest = store.load_manifest()
            self.assertEqual(manifest["titles_status"], "done")
            self.assertTrue(all(
                chapter["title_translated"] == " ".join(chapter["title"].split())
                for chapter in manifest["chapters"]
            ))


class TestReport(unittest.TestCase):
    def test_report_summary(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            g = GlossaryStore(store.glossary_path)
            report = build_report(store, g)
            g.close()
            s = report["summary"]
            self.assertEqual(s["chapters_done"], s["chapters_total"])
            self.assertEqual(s["glossary_pending"], 0)
            self.assertFalse(s["titles_pending"])
            self.assertEqual(s["empty_targets"], 0)  # 全部段都有译文
            self.assertGreaterEqual(s["terms"], 1)

            manifest = store.load_manifest()
            manifest["chapters"][0]["glossary_status"] = "pending"
            manifest["titles_status"] = "pending"
            store.save_manifest(manifest)
            g = GlossaryStore(store.glossary_path)
            pending_summary = build_report(store, g)["summary"]
            self.assertEqual(pending_summary["glossary_pending"], 1)
            self.assertTrue(pending_summary["titles_pending"])
            g.close()


class TestConsistency(unittest.TestCase):
    def test_consistency_reports_issues(self):
        from trans_novel.agents.consistency import ConsistencyChecker

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, cfg = _run(txt, os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "一致性审查员" in messages[0]["content"]:
                    return json.dumps({"issues": [
                        {"type": "terminology", "detail": "X 译法不一致", "where": "第1章"}
                    ]}, ensure_ascii=False)
                return "{}"

            g = GlossaryStore(store.glossary_path)
            checker = ConsistencyChecker(FakeClient(handler=handler), cfg)
            issues = checker.check(store, g)
            g.close()
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["type"], "terminology")

    def test_autofix_rejects_non_string_replacements(self):
        from trans_novel.agents.consistency import ConsistencyChecker

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, cfg = _run(txt, os.path.join(d, "state"))
            before = [
                segment.target
                for chapter in store.load_manifest()["chapters"]
                for segment in store.load_chapter(chapter["index"]).text_segments
            ]

            def handler(messages, tier, json_mode):
                return json.dumps({"replacements": [
                    {"wrong": "润", "right": {"bad": True}},
                    {"wrong": ["bad"], "right": "正确"},
                    {"wrong": "润", "right": None},
                ]}, ensure_ascii=False)

            glossary = store.open_glossary()
            result = ConsistencyChecker(
                FakeClient(handler=handler), cfg
            ).autofix(store, glossary)
            glossary.close()
            after = [
                segment.target
                for chapter in store.load_manifest()["chapters"]
                for segment in store.load_chapter(chapter["index"]).text_segments
            ]
            self.assertEqual(result, {"replacements": [], "rewritten": 0})
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
