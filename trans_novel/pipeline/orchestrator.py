"""编排器：驱动全流程，章级状态机 + 断点续跑。

单章流水线（章内批次**串行**，逐批刷新滚动上下文与术语快照；跨章亦串行传递梗概）：
  每批：渲染上下文（含前一批刚译出的译文）→ 翻译（对齐保证）→ 润色（可选）→
        标点规范化 → 术语/称呼/固定表达实时抽取入库 → 立即供下一批参照。
  章末（串行）：全章术语兜底抽取 → 整章分块审校（不阻塞翻译主路径）→
        严重项定向重译（autofix_severe，过长度校验才采纳）→ 回译抽检 → 写 TM → 落盘标记 done。
翻译前先预扫源文建立全书理解（逐章梗概+全书概览，fast 档并行），作恒定前缀注入每章翻译。

run_all：在翻译全书后接 术语 AI 审计统一 → 一致性 QA → 写报告 → 回填出 EPUB，一气呵成。
进度回调 progress(done_segments, total_segments, label) 与 UI 无关，每批完成即触发。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Config
from ..glossary.extractor import (
    GlossaryExtractionError,
    GlossaryExtractor,
    GlossaryPersistenceError,
)
from ..glossary.store import GlossaryCheckpoint, GlossaryStore, TYPE_PERSON
from ..llm.base import LLMClient, build_client
from ..ingest.segmenter import load_document, batch_segments
from ..postprocess.punct import normalize_zh
from ..agents.analyzer import Analyzer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..agents.reviewer import Reviewer, BackTranslator
from ..agents.polisher import Polisher
from . import checks
from .context import RollingContext
from .glossary_windows import (
    batch_glossary_checkpoints,
    chapter_glossary_window_checkpoints,
    chapter_glossary_windows,
    expected_chapter_glossary_plan,
    reconcile_chapter_glossary,
)
from .runstore import (
    GLOSSARY_DONE,
    GLOSSARY_PENDING,
    RunStore,
    STATUS_DONE,
    STATUS_PENDING,
    slugify,
    source_fingerprint,
    translation_fingerprint,
)

ProgressFn = Callable[[int, int, str], None]
_GLOSSARY_PLAN_VERSION = 1


# 语言名/代码 → ISO 639-1 两字母代码（模型检测结果归一化）
_LANG_ALIASES = {
    "japanese": "ja", "日语": "ja", "日文": "ja", "jp": "ja", "jpn": "ja",
    "english": "en", "英语": "en", "英文": "en", "eng": "en",
    "russian": "ru", "俄语": "ru", "俄文": "ru", "rus": "ru",
    "chinese": "zh", "中文": "zh", "汉语": "zh", "zh-cn": "zh", "zho": "zh",
    "korean": "ko", "韩语": "ko", "韩文": "ko", "kor": "ko",
    "french": "fr", "法语": "fr", "法文": "fr",
    "german": "de", "德语": "de", "德文": "de",
    "spanish": "es", "西班牙语": "es", "西班牙文": "es",
    "italian": "it", "意大利语": "it", "意大利文": "it",
    "portuguese": "pt", "葡萄牙语": "pt", "葡萄牙文": "pt",
}

_FRONT_MATTER_TITLE_PATTERNS = (
    "cover",
    "copyright",
    "title page",
    "contents",
    "table of contents",
    "dedication",
    "acknowledgments",
    "acknowledgements",
    "about the author",
    "bibliography",
    "references",
    "index",
)


def _normalize_lang(code: str) -> str:
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}:
        return ""
    if c in _LANG_ALIASES:
        return _LANG_ALIASES[c]
    return c[:2] if c[:2].isalpha() else ""


@dataclass
class _BatchResult:
    targets: list[str]
    issues: list[dict] = field(default_factory=list)


class Orchestrator:
    def __init__(self, config: Config, client: LLMClient | None = None):
        self.config = config
        self.client = client or build_client(config)
        self.analyzer = Analyzer(self.client, config)
        self.synopsizer = Synopsizer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.backtrans = BackTranslator(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)

    # ── 语言解析 ────────────────────────────────────────────────────────────
    def _apply_language(self, lang: str) -> None:
        """把解析出的源语言应用到 config 与各 agent（auto 检测后调用）。"""
        resolved = lang or self.config.source_lang
        self.config.source_lang = resolved
        for ag in (self.analyzer, self.synopsizer, self.translator, self.reviewer,
                   self.backtrans, self.polisher, self.extractor):
            ag.src = resolved

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def prepare(self, input_path: str) -> RunStore:
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回
        doc = load_document(input_path, self.config.source_lang, self.config.target_lang,
                            split_segments=self.config.segment.max_chars_per_segment)
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        if store.exists():
            manifest = store.load_manifest()
            self._apply_language(manifest.get("source_lang") or self.config.source_lang)
            glossary = store.open_glossary()
            try:
                self._repair_existing_run(store, input_path, glossary)
                self._reconcile_translation_statuses(store)
                self._reconcile_glossary_statuses(store, glossary)
            finally:
                glossary.close()
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # 已有进度 → 直接续跑，不重置（语言在 run() 里按 manifest 应用）

        artifacts = store.orphan_artifacts()
        if artifacts:
            raise RuntimeError(
                f"状态目录缺少 manifest.json，但仍有 {len(artifacts)} 个残留产物；"
                "为避免复用来源不明的术语库，已停止初始化。请先备份并人工核对该目录。"
            )

        # 新建：auto 时只使用模型检测主要语言；失败则要求用户显式指定。
        detected_source_lang = ""
        if self.config.source_lang in ("auto", "", None):
            detected = self._detect_language_ai(doc)
            if not detected:
                raise RuntimeError(
                    "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 "
                    "language.source 指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
                )
            doc.source_lang = detected
            detected_source_lang = detected
        self._apply_language(doc.source_lang)

        glossary = GlossaryStore(store.glossary_path)
        store.init_from_document(
            doc,
            glossary_generation_id=glossary.generation_id,
        )
        store.log_event(
            "run_initialized",
            input_path=input_path,
            run_dir=store.run_dir,
            title=doc.title,
            fmt=doc.fmt,
            source_lang=doc.source_lang,
            target_lang=doc.target_lang,
            chapters=len(doc.chapters),
            config={
                "review": self.config.pipeline.review,
                "autofix_severe": self.config.pipeline.autofix_severe,
                "polish": self.config.pipeline.polish,
                "backtranslate_sample": self.config.pipeline.backtranslate_sample,
                "consistency_qa": self.config.pipeline.consistency_qa,
                "book_understanding": self.config.pipeline.book_understanding,
            },
        )
        if detected_source_lang:
            store.log_event(
                "language_detected", source_lang=detected_source_lang
            )
        analysis: dict[str, Any] = {}
        try:
            sample = self._sample_text(doc)
            try:
                analysis = self.analyzer.analyze(sample) if sample else {}
            except Exception as exc:
                # 模型拒答/非 JSON 只关闭质量增强，不掩盖 SQLite 故障。
                analysis = {}
                store.log_event("analysis_failed", error=str(exc)[:500])
            # analysis.json is the retry source of truth. Keep the manifest status
            # pending until the same saved payload has been seeded successfully.
            store.save_analysis(analysis)
            store.log_event("analysis_saved", has_analysis=bool(analysis))
            if analysis:
                try:
                    self.analyzer.seed_glossary(glossary, analysis)
                except Exception as exc:
                    store.log_event(
                        "analysis_glossary_seed_failed",
                        error=f"{type(exc).__name__}: {exc}"[:500],
                    )
                    raise
            store.set_analysis_glossary_status(GLOSSARY_DONE)
        finally:
            glossary.close()
        store.save_context(RollingContext().to_dict())
        return store

    def _repair_existing_run(
        self,
        store: RunStore,
        input_path: str,
        glossary: GlossaryStore,
    ) -> None:
        """补齐早期中断留下的缺失产物，不重置已有章节译文和 manifest。"""
        if not os.path.isfile(store.context_path):
            store.save_context(RollingContext().to_dict())
            store.log_event("context_repaired", reason="missing")

        if os.path.isfile(store.analysis_path):
            manifest = store.load_manifest()
            if manifest.get("analysis_glossary_status") != GLOSSARY_PENDING:
                return
            loaded_analysis = store.load_analysis()
            analysis = loaded_analysis if isinstance(loaded_analysis, dict) else {}
            try:
                if analysis:
                    self.analyzer.seed_glossary(glossary, analysis)
                store.set_analysis_glossary_status(GLOSSARY_DONE)
                store.log_event(
                    "analysis_glossary_seed_repaired",
                    term_count=(
                        len(self.analyzer.dict_items(analysis.get("characters")))
                        + len(self.analyzer.dict_items(analysis.get("terms")))
                    ),
                )
            except Exception as exc:
                store.log_event(
                    "analysis_glossary_seed_failed",
                    phase="repair_saved_analysis",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                raise
            return

        analysis: dict[str, Any] = {}
        store.set_analysis_glossary_status(GLOSSARY_PENDING)
        doc = load_document(
            input_path,
            self.config.source_lang,
            self.config.target_lang,
            split_segments=self.config.segment.max_chars_per_segment,
        )
        sample = self._sample_text(doc)
        try:
            analysis = self.analyzer.analyze(sample) if sample else {}
        except Exception as exc:
            analysis = {}
            store.log_event(
                "analysis_failed", phase="repair", error=str(exc)[:500]
            )
        store.save_analysis(analysis)
        store.log_event("analysis_repaired", has_analysis=bool(analysis))
        if analysis:
            try:
                self.analyzer.seed_glossary(glossary, analysis)
            except Exception as exc:
                store.log_event(
                    "analysis_glossary_seed_failed",
                    phase="repair",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                raise
        store.set_analysis_glossary_status(GLOSSARY_DONE)

    def _reconcile_glossary_statuses(
        self,
        store: RunStore,
        glossary: GlossaryStore,
    ) -> None:
        """Validate new-state markers and reopen done chapters on checkpoint drift."""
        manifest = store.load_manifest()
        for entry in manifest.get("chapters", []):
            chapter = store.load_chapter(entry["index"])
            has_plan = "glossary_plan" in chapter.meta
            has_chapter_plan = "chapter_glossary_plan" in chapter.meta
            has_status = "glossary_status" in entry
            has_legacy_marker = "glossary_legacy" in entry
            if has_legacy_marker and (
                entry.get("glossary_legacy") is not True
                or has_status
                or has_plan
            ):
                store.log_event(
                    "chapter_glossary_state_invalid",
                    chapter=chapter.index,
                    reason="invalid_legacy_marker_combination",
                )
                raise RuntimeError(
                    f"第 {chapter.index} 章 legacy 迁移标记与新术语状态冲突；"
                    "已停止续跑。"
                )
            if not has_status and not has_plan:
                if entry.get("glossary_legacy") is True:
                    continue
                store.log_event(
                    "chapter_glossary_state_invalid",
                    chapter=chapter.index,
                    reason="markers_missing_without_legacy_evidence",
                )
                raise RuntimeError(
                    f"第 {chapter.index} 章缺少术语 plan/status 且没有 legacy 迁移证据；"
                    "已停止续跑。"
                )
            status = entry.get("glossary_status")
            if not has_status or status not in {GLOSSARY_PENDING, GLOSSARY_DONE}:
                store.log_event(
                    "chapter_glossary_state_invalid",
                    chapter=chapter.index,
                    reason="missing_or_invalid_status",
                )
                raise RuntimeError(
                    f"第 {chapter.index} 章术语状态标记缺失或非法；"
                    "为避免误判 legacy，已停止续跑。"
                )
            if not has_plan:
                if status == GLOSSARY_PENDING or not chapter.text_segments:
                    continue
                store.log_event(
                    "chapter_glossary_state_invalid",
                    chapter=chapter.index,
                    reason="done_without_plan",
                )
                raise RuntimeError(
                    f"第 {chapter.index} 章术语标记为 done 但缺少 canonical plan；"
                    "已停止续跑。"
                )
            units = self._canonical_glossary_units(
                chapter, chapter.text_segments, store
            )
            chapter_plan = None
            if has_chapter_plan:
                chapter_plan = self._chapter_glossary_windows(
                    chapter,
                    chapter.text_segments,
                    units,
                    store,
                    create=False,
                )
            if status == GLOSSARY_PENDING:
                continue
            complete = all(
                segment.target and segment.target.strip()
                for segment in chapter.text_segments
            )
            if complete:
                for unit in units:
                    start = unit["start_index"]
                    segments = chapter.text_segments[start:start + unit["count"]]
                    if not glossary.checkpoint_matches(GlossaryCheckpoint(
                        scope="batch",
                        chapter=chapter.index,
                        start_index=start,
                        count=len(segments),
                        fingerprint=translation_fingerprint(segments),
                    )):
                        complete = False
                        break
            if complete and chapter_plan is None:
                complete = glossary.checkpoint_matches(GlossaryCheckpoint(
                    scope="chapter",
                    chapter=chapter.index,
                    start_index=0,
                    count=len(chapter.text_segments),
                    fingerprint=translation_fingerprint(chapter.text_segments),
                ))
            elif complete:
                batches = self._batch_glossary_checkpoints(
                    chapter.index, chapter.text_segments, units
                )
                windows = self._chapter_glossary_window_checkpoints(
                    chapter.index, chapter.text_segments, chapter_plan
                )
                complete = glossary.chapter_completion_matches_v2(
                    chapter=chapter.index,
                    batch_checkpoints=batches,
                    window_checkpoints=windows,
                    plan_fingerprint=chapter_plan["fingerprint"],
                )
            if complete:
                continue
            store.set_chapter_glossary_status(chapter.index, GLOSSARY_PENDING)
            store.log_event(
                "chapter_glossary_reopened",
                chapter=chapter.index,
                reason="checkpoint_missing_or_mismatch",
            )

    def _reconcile_translation_statuses(self, store: RunStore) -> None:
        """Migrate legacy done chapters with holes; reject the same in new state."""
        manifest = store.load_manifest()
        reopened: list[tuple[int, list[int]]] = []
        for entry in manifest.get("chapters", []):
            if entry.get("status") != STATUS_DONE:
                continue
            chapter = store.load_chapter(entry["index"])
            missing = [
                index
                for index, segment in enumerate(chapter.text_segments)
                if not (segment.target and segment.target.strip())
            ]
            if not missing:
                continue
            has_plan = "glossary_plan" in chapter.meta
            is_legacy = (
                entry.get("glossary_legacy") is True
                and "glossary_status" not in entry
                and not has_plan
            )
            if not is_legacy:
                store.log_event(
                    "chapter_state_invalid",
                    chapter=chapter.index,
                    reason="done_with_missing_target",
                    missing_indices=missing,
                )
                raise RuntimeError(
                    f"第 {chapter.index} 章标记为 done 但仍有空译文；"
                    "为保护新格式状态，已停止续跑。"
                )
            entry["status"] = STATUS_PENDING
            entry["glossary_status"] = GLOSSARY_PENDING
            entry.pop("glossary_legacy", None)
            reopened.append((chapter.index, missing))

        if not reopened:
            return
        manifest["titles_status"] = STATUS_PENDING
        manifest.pop("title_translated", None)
        for entry in manifest.get("chapters", []):
            if isinstance(entry, dict):
                entry.pop("title_translated", None)
        meta = manifest.get("meta")
        toc_entries = meta.get("toc_entries") if isinstance(meta, dict) else None
        if isinstance(toc_entries, list):
            for entry in toc_entries:
                if isinstance(entry, dict):
                    entry.pop("title_translated", None)
        store.save_manifest(manifest)
        for chapter, missing in reopened:
            store.log_event(
                "legacy_chapter_translation_reopened",
                chapter=chapter,
                missing_indices=missing,
                reason="done_with_missing_target",
            )

    def _detect_language_ai(self, doc) -> str:
        """用模型检测正文主要语言，返回 ISO 代码（如 ja/en/ru）。失败返回空串。"""
        # labeled=False：纯源文样本，防多点采样的中文标签污染语言检测
        sample = self._sample_text(doc, labeled=False)[:1500]
        if not sample.strip():
            return ""
        system = (
            "你是语言识别器。判断给定文本的主要自然语言，"
            '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
            "无法判断时 language 置为空字符串。"
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": sample}], tier="cheap")
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return _normalize_lang(str(code))
        except Exception:
            return ""

    @staticmethod
    def _sample_text(doc, *, labeled: bool = True) -> str:
        """取风格分析样章。labeled=True 时多点采样（开头/中部/结尾各一段，带中文标注），
        让分析覆盖全书风格全貌；labeled=False 返回单段纯源文（语言检测用，不能混入中文标签）。"""
        def _chapter_texts(*, include_front_matter: bool) -> list[str]:
            out: list[str] = []
            for ch in doc.chapters:
                text = "\n".join(s.source for s in ch.text_segments)
                if len(text) <= 200:
                    continue
                if not include_front_matter and Orchestrator._looks_front_matter(
                    ch.title, text
                ):
                    continue
                out.append(text)
            return out

        texts = _chapter_texts(include_front_matter=False)
        if not texts:
            texts = _chapter_texts(include_front_matter=True)
        if not texts:  # 兜底：全书都是短章
            joined = "\n".join(
                s.source for ch in doc.chapters[:2] for s in ch.text_segments)
            return joined[:6000]
        if not labeled:
            return texts[0][:6000]
        picks = [(0, "开头样章"), (len(texts) // 2, "中部样章"), (len(texts) - 1, "结尾样章")]
        parts: list[str] = []
        seen: set[int] = set()
        for idx, tag in picks:
            if idx in seen:  # 短书（1-2 章）去重，不重复取同一章
                continue
            seen.add(idx)
            t = texts[idx]
            chunk = t[-2800:] if tag == "结尾样章" else t[:2800]
            parts.append(f"【{tag}】\n{chunk}")
        return "\n\n".join(parts)

    @staticmethod
    def _looks_front_matter(title: str, text: str) -> bool:
        """分析采样避开版权页/目录页等前置材料，减少模型拒答和风格误判。"""
        title_l = (title or "").strip().lower()
        if any(p in title_l for p in _FRONT_MATTER_TITLE_PATTERNS):
            return True
        head = (text or "")[:400].lower()
        return (
            "copyright ©" in head
            or "all rights reserved" in head
            or "isbn:" in head
            or "table of contents" in head
        )

    def run(self, input_path: str, *, only_chapter: int | None = None,
            progress: Optional[ProgressFn] = None) -> RunStore:
        store = self.prepare(input_path)
        manifest = store.load_manifest()
        self._apply_language(manifest.get("source_lang") or self.config.source_lang)
        had_glossary_pending = any(
            chapter.get("glossary_status") == GLOSSARY_PENDING
            for chapter in manifest.get("chapters", [])
        )
        if had_glossary_pending:
            store.invalidate_titles()
            store.log_event("titles_invalidated", reason="glossary_pending")
        glossary = store.open_glossary()
        style = self.analyzer.style_brief(store.load_analysis() or {})
        legacy_glossary = store.legacy_batch_glossary_evidence()
        # 翻译前预扫源文，建立全书理解（幂等、可续跑）；全书概览注入每章翻译
        book_synopsis = self._build_understanding(store)

        if only_chapter is not None:
            targets = [only_chapter]
        else:
            targets = store.pending_work_chapters()

        total = self._count_segments(store, targets)
        done = 0
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
        )
        try:
            for ci in targets:
                context = self._context_before_chapter(store, ci)
                done = self._translate_chapter(
                    ci, store, glossary, context, style, book_synopsis,
                    progress=progress, done=done, total=total,
                    legacy_glossary=legacy_glossary)
                # context.json 是可重建缓存；最终章节 JSON 才是 target 权威来源。
                store.save_context(
                    self._context_before_chapter(store, ci + 1).to_dict()
                )
            # 全书译完后翻译各章标题和目录项（书名保持原文，借术语表保持专名一致）
            if not store.pending_work_chapters():
                self._translate_titles(
                    store,
                    glossary,
                    force=had_glossary_pending,
                )
        finally:
            glossary.close()
        if progress and total:
            progress(total, total, "翻译完成")
        store.log_event("translate_run_finished", total_segments=total)
        return store

    @staticmethod
    def _count_segments(store: RunStore, chapter_indices: list[int]) -> int:
        total = 0
        for ci in chapter_indices:
            total += len(store.load_chapter(ci).text_segments)
        return total

    @staticmethod
    def _context_before_chapter(store: RunStore, chapter_index: int) -> RollingContext:
        """从已完成章节重建局部上下文，避免跨文件写入窗口造成 stale cache。"""
        context = RollingContext()
        manifest = store.load_manifest()
        for entry in manifest.get("chapters", []):
            index = entry.get("index")
            if not isinstance(index, int) or index >= chapter_index:
                continue
            if entry.get("status") != STATUS_DONE:
                continue
            chapter = store.load_chapter(index)
            targets = [segment.target or "" for segment in chapter.text_segments]
            if any(not target.strip() for target in targets):
                store.log_event(
                    "chapter_state_invalid",
                    chapter=index,
                    reason="done_with_missing_target_during_context_rebuild",
                )
                raise RuntimeError(
                    f"第 {index} 章标记为 done 但仍有空译文；"
                    "无法安全重建滚动上下文。"
                )
            context.add_targets(targets)
        return context

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────────
    def _build_understanding(self, store: RunStore) -> str:
        """翻译前预扫源文：逐章梗概存入 chapter.meta，归并出全书概览存入 analysis。

        幂等、可续跑：已有梗概/概览则跳过。返回全书概览（注入各章翻译 prompt）。
        关闭 book_understanding 时直接返回空串。
        """
        if not self.config.pipeline.book_understanding:
            store.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = store.load_manifest()
        chapters = manifest.get("chapters", [])

        # 各章梗概相互独立 → 并行调用（LLM 调用进线程池；落盘全部在主线程，
        # 保持原子写不竞争，且逐章增量落盘、续跑粒度不变）。已有梗概的章跳过（幂等）。
        loaded = {c.get("index", i): store.load_chapter(c.get("index", i))
                  for i, c in enumerate(chapters)}
        todo = []
        for ci, ch in loaded.items():
            if ch.meta.get("source_digest"):
                continue
            src = "\n".join(s.source for s in ch.text_segments)
            if self._looks_front_matter(ch.title, src):
                continue
            todo.append((ci, src))
        if todo:
            store.log_event(
                "book_understanding_chapter_digest_started",
                chapters=[ci for ci, _ in todo],
                workers=max(1, self.config.pipeline.prescan_concurrency),
            )
            workers = max(1, self.config.pipeline.prescan_concurrency)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.synopsizer.digest_chapter, src): ci
                        for ci, src in todo}
                for fut in as_completed(futs):
                    ci = futs[fut]
                    loaded[ci].meta["source_digest"] = fut.result()  # 失败时 _ask_text 已回退 ""
                    store.save_chapter(loaded[ci])
                    store.log_event(
                        "book_understanding_chapter_digest_saved",
                        chapter=ci,
                        digest=loaded[ci].meta["source_digest"],
                    )

        # 按 manifest 章序组装（与并发完成顺序无关）
        digests = [loaded[c.get("index", i)].meta.get("source_digest", "") or ""
                   for i, c in enumerate(chapters)]

        analysis = store.load_analysis() or {}
        synopsis = analysis.get("book_synopsis", "")
        if not synopsis and any(d.strip() for d in digests):
            synopsis = self.synopsizer.book_synopsis(
                digests, self.analyzer.style_brief(analysis))
            analysis["book_synopsis"] = synopsis
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis

    # ── 章节标题 / 目录项翻译（书名保持原文）──────────────────────────────
    def _translate_titles(
        self,
        store: RunStore,
        glossary: GlossaryStore,
        *,
        force: bool = False,
    ) -> None:
        """把各章标题和额外目录项翻成中文，写回 manifest（幂等：已全部译过则跳过）。

        书名保持原文，不写 title_translated；借术语表保证章节标题里的专名一致。
        """
        from ..agents import prompts

        m = store.load_manifest()
        chapters = m.get("chapters", [])
        force = force or m.get("titles_status") == STATUS_PENDING

        # 标题压成单行，避免内嵌换行破坏 numbered 对齐
        def _flat(s: object) -> str:
            return " ".join(str(s or "").split())

        raw_meta = m.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        chapter_hrefs = {c.get("href") for c in chapters if c.get("href")}
        raw_toc_entries = meta.get("toc_entries", [])
        toc_entry_items = raw_toc_entries if isinstance(raw_toc_entries, list) else []
        toc_entries = [
            e for e in toc_entry_items
            if isinstance(e, dict) and e.get("href") not in chapter_hrefs and _flat(e.get("title", ""))
        ]

        titled_chapters = [c for c in chapters if _flat(c.get("title", ""))]
        m.pop("title_translated", None)
        if (not force
                and all(c.get("title_translated") for c in titled_chapters)
                and all(e.get("title_translated") for e in toc_entries)):
            m["titles_status"] = STATUS_DONE
            store.save_manifest(m)
            store.log_event("titles_skipped", reason="already_translated")
            return  # 已译，断点续跑不重复调用

        titles = (
            [_flat(c.get("title", "")) for c in titled_chapters]
            + [_flat(e.get("title", "")) for e in toc_entries]
        )
        if not any(t.strip() for t in titles):
            m["titles_status"] = STATUS_DONE
            store.save_manifest(m)
            return
        all_terms = glossary.all_terms()
        title_terms = GlossaryStore.terms_in(all_terms, "\n".join(titles))
        system = prompts.render("title_translator_system",
                                src=self.config.source_lang, tgt=self.config.target_lang,
                                n=len(titles))
        user = prompts.render("title_translator_user",
                              src=self.config.source_lang, tgt=self.config.target_lang,
                              glossary=prompts.render_glossary(title_terms),
                              n=len(titles), numbered_titles=prompts.numbered(titles))
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}], tier="strong")
        except Exception as exc:
            store.log_event(
                "titles_translation_failed",
                reason="exception",
                error=str(exc),
            )
            return
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != len(titles):
            store.log_event(
                "titles_translation_rejected",
                reason="count_mismatch",
                expected=len(titles),
                actual=len(out) if isinstance(out, list) else None,
            )
            return
        out = [
            candidate.strip()
            if isinstance(candidate, str) and candidate.strip()
            else source
            for source, candidate in zip(titles, out)
        ]
        chapter_out = out[:len(titled_chapters)]
        toc_out = out[len(titled_chapters):]
        for c, t in zip(titled_chapters, chapter_out):
            c["title_translated"] = t or c.get("title")
        for e, t in zip(toc_entries, toc_out):
            e["title_translated"] = t or e.get("title")
        m["titles_status"] = STATUS_DONE
        store.save_manifest(m)
        store.log_event(
            "titles_translated",
            reference_terms_total=len(all_terms),
            reference_terms_selected=len(title_terms),
            titles=[
                {"index": i - 1, "source": src, "target": tgt}
                for i, (src, tgt) in enumerate(zip(titles, out))
            ],
        )

    # ── 单章 ──────────────────────────────────────────────────────────────
    def _translate_chapter(self, ci: int, store: RunStore,
                           glossary: GlossaryStore, context: RollingContext,
                           style: str, book_synopsis: str = "", *,
                           progress: Optional[ProgressFn] = None,
                           done: int = 0, total: int = 0,
                           legacy_glossary: Optional[
                               dict[tuple[int, int, int], str]
                           ] = None) -> int:
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.set_chapter_glossary_status(ci, GLOSSARY_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        manifest = store.load_manifest()
        saved_target_indices = {
            index
            for index, segment in enumerate(text_segs)
            if segment.target and segment.target.strip()
        }
        translation_was_done = next(
            c["status"] == STATUS_DONE
            for c in manifest["chapters"]
            if c["index"] == ci
        )
        if translation_was_done and not all(
            segment.target and segment.target.strip() for segment in text_segs
        ):
            store.log_event(
                "chapter_state_invalid",
                chapter=ci,
                reason="done_with_missing_target",
            )
            raise RuntimeError(
                f"第 {ci} 章标记为 done 但仍有空译文；为保护状态，已停止续跑。"
            )
        store.set_chapter_glossary_status(ci, GLOSSARY_PENDING)
        chapter_digest = chapter.meta.get("source_digest", "")

        units = self._canonical_glossary_units(chapter, text_segs, store)
        label = f"第{ci}章 {chapter.title}"
        term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
        review_issues: list[dict] = [
            i for i in chapter.meta.get("review_issues", [])
            if isinstance(i, dict) and i.get("stage") != "review"
        ]
        glossary_complete = True
        legacy_glossary = legacy_glossary or {}

        # 术语单元固定；译文工作在单元内按已完成/缺失连续段切分。
        for unit in units:
            unit_start = unit["start_index"]
            batch = text_segs[unit_start:unit_start + unit["count"]]
            for run_offset, run, already_done in self._translation_runs(batch):
                run_start = unit_start + run_offset
                if already_done:
                    targets = [s.target or "" for s in run]
                    if not translation_was_done:
                        context.add_targets(targets)
                    store.log_event(
                        "batch_skipped",
                        chapter=ci,
                        start_index=run_start,
                        count=len(run),
                        reason="already_translated",
                    )
                else:
                    ctx_text = context.render(
                        self.config.pipeline.rolling_context_segments
                    )
                    res = self._process_batch(
                        run, term_snapshot, ctx_text, style,
                        book_synopsis, chapter_digest,
                    )
                    for segment, target in zip(run, res.targets):
                        segment.target = target
                    store.log_event(
                        "batch_translated",
                        chapter=ci,
                        start_index=run_start,
                        count=len(run),
                        polished=self.config.pipeline.polish,
                        punctuation_normalized=self.config.punctuation_normalize,
                        issues=res.issues,
                        segments=[
                            {
                                "index": run_start + i,
                                "source": segment.source,
                                "target": target,
                            }
                            for i, (segment, target) in enumerate(
                                zip(run, res.targets)
                            )
                        ],
                    )
                    context.add_targets(res.targets)
                    for issue in res.issues:
                        issue["chapter"] = ci
                        issue["index"] += run_start
                    review_issues.extend(res.issues)
                    chapter.meta["review_issues"] = review_issues
                    store.save_chapter(chapter)
                done += len(run)
                if progress:
                    progress(done, total, label)

            if not all(s.target and s.target.strip() for s in batch):
                glossary_complete = False
                continue
            fingerprint = translation_fingerprint(batch)
            checkpoint = GlossaryCheckpoint(
                scope="batch",
                chapter=ci,
                start_index=unit_start,
                count=len(batch),
                fingerprint=fingerprint,
            )
            key = (ci, unit_start, len(batch))
            changed = False
            if glossary.checkpoint_matches(checkpoint):
                store.log_event(
                    "batch_glossary_skipped",
                    chapter=ci,
                    start_index=unit_start,
                    count=len(batch),
                    fingerprint=fingerprint,
                    reason="checkpoint_match",
                )
            elif legacy_glossary.get(key) == fingerprint:
                glossary.record_checkpoint(checkpoint)
                store.log_event(
                    "batch_glossary_skipped",
                    chapter=ci,
                    start_index=unit_start,
                    count=len(batch),
                    fingerprint=fingerprint,
                    reason="legacy_checkpoint_promoted",
                    checkpoint_version=checkpoint.version,
                    generation_id=glossary.generation_id,
                )
            else:
                summary = self._extract_glossary(
                    glossary, store, ci, batch,
                    phase="batch", checkpoint=checkpoint,
                )
                glossary_complete &= summary is not None
                changed = summary is not None
            if changed:
                term_snapshot = self._chapter_term_snapshot(glossary, text_segs)

        if all(s.target and s.target.strip() for s in text_segs):
            chapter_complete, chapter_changed = self._reconcile_chapter_glossary(
                glossary,
                store,
                chapter,
                text_segs,
                units,
            )
            glossary_complete &= chapter_complete
            if chapter_changed:
                term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
        else:
            glossary_complete = False

        if translation_was_done:
            store.set_chapter_glossary_status(
                ci, GLOSSARY_DONE if glossary_complete else GLOSSARY_PENDING
            )
            store.log_event(
                "chapter_glossary_reconciled",
                chapter=ci,
                completed=glossary_complete,
            )
            return done

        targets_before_review = [segment.target for segment in text_segs]

        # ── 章末整章审校（移出批内关键路径；块内 index 映射回章内段号）──
        # 幂等：续跑重入章末时清掉旧审校项，防重复累积。
        if self.config.pipeline.review:
            review_issues = [
                i for i in review_issues
                if i.get("stage") != "review"
            ]
            new_issues = self._review_chapter(text_segs, term_snapshot)
            store.log_event(
                "chapter_reviewed",
                chapter=ci,
                issue_count=len(new_issues),
                issues=new_issues,
            )
            if self.config.pipeline.autofix_severe:
                self._autofix_severe(text_segs, new_issues, term_snapshot, style,
                                     book_synopsis, chapter_digest,
                                     store=store, chapter_index=ci,
                                     mutable_indices=(
                                         set(range(len(text_segs)))
                                         - saved_target_indices
                                     ))
            for it in new_issues:
                it["chapter"] = ci
                it.setdefault("fixed", False)
                it["stage"] = "review"
            review_issues.extend(new_issues)

        changed_targets = {
            index
            for index, (before, segment) in enumerate(
                zip(targets_before_review, text_segs)
            )
            if before != segment.target
        }
        if changed_targets:
            chapter.meta["review_issues"] = review_issues
            store.save_chapter(chapter)
            glossary_complete, term_snapshot = self._refresh_changed_glossary(
                glossary,
                store,
                chapter,
                text_segs,
                units,
                changed_targets,
                glossary_complete,
                term_snapshot,
            )

        # 回译抽检
        bt_issues: list[dict] = []
        bt_sample_indices = self._backtranslation_sample_indices(ci, text_segs)
        if bt_sample_indices:
            srcs = [text_segs[index].source for index in bt_sample_indices]
            tgts = [text_segs[index].target or "" for index in bt_sample_indices]
            for it in self.backtrans.check(srcs, tgts):
                sample_index = it.get("index")
                if isinstance(sample_index, int) and not isinstance(sample_index, bool):
                    if not 0 <= sample_index < len(bt_sample_indices):
                        continue
                    it["index"] = bt_sample_indices[sample_index]
                it["chapter"] = ci
                bt_issues.append(it)
            store.log_event(
                "chapter_backtranslation_checked",
                chapter=ci,
                sample_count=len(bt_sample_indices),
                sample_indices=bt_sample_indices,
                issue_count=len(bt_issues),
                issues=bt_issues,
            )

        # 翻译记忆库（仅作记录/参考，不用于跨位置复用译文）
        for s in text_segs:
            if s.target:
                glossary.add_tm(s.source, s.target, ci)

        chapter.meta["review_issues"] = review_issues
        chapter.meta["backtranslation_issues"] = bt_issues
        store.save_chapter(chapter)
        store.set_chapter_glossary_status(
            ci, GLOSSARY_DONE if glossary_complete else GLOSSARY_PENDING
        )
        if all(s.target and s.target.strip() for s in text_segs):
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event(
                "chapter_done",
                chapter=ci,
                title=chapter.title,
                segment_count=len(text_segs),
                review_issue_count=len(review_issues),
                backtranslation_issue_count=len(bt_issues),
            )
        else:
            store.log_event(
                "chapter_translation_incomplete",
                chapter=ci,
                remaining=sum(
                    not (s.target and s.target.strip()) for s in text_segs
                ),
                review_issue_count=len(review_issues),
            )
        return done

    def _backtranslation_sample_indices(self, chapter: int, text_segs) -> list[int]:
        """Select a stable sample from final targets so resume cannot skip QA."""
        rate = self.config.pipeline.backtranslate_sample
        if rate <= 0:
            return []
        if rate >= 1:
            return list(range(len(text_segs)))
        threshold = int(rate * (1 << 64))
        selected: list[int] = []
        for index, segment in enumerate(text_segs):
            digest = source_fingerprint([
                str(chapter),
                str(index),
                segment.source,
            ])
            if int(digest[:16], 16) < threshold:
                selected.append(index)
        return selected

    def _canonical_glossary_units(self, chapter, text_segs, store: RunStore) -> list[dict]:
        """建立一次后固定术语批次；续跑时只校验，不随配置或 target 重分批。"""
        has_plan = "glossary_plan" in chapter.meta
        plan = chapter.meta.get("glossary_plan")
        if not has_plan:
            units: list[dict[str, Any]] = []
            start = 0
            for batch in batch_segments(
                text_segs, self.config.segment.max_chars_per_batch
            ):
                units.append({
                    "start_index": start,
                    "count": len(batch),
                    "source_fingerprint": source_fingerprint(
                        [segment.source for segment in batch]
                    ),
                })
                start += len(batch)
            plan = {"version": _GLOSSARY_PLAN_VERSION, "units": units}
            chapter.meta["glossary_plan"] = plan
            store.save_chapter(chapter)
            store.log_event(
                "glossary_plan_created",
                chapter=chapter.index,
                version=_GLOSSARY_PLAN_VERSION,
                unit_count=len(units),
            )

        try:
            if not isinstance(plan, dict) or plan.get("version") != _GLOSSARY_PLAN_VERSION:
                raise ValueError("unsupported plan version")
            units = plan.get("units")
            if not isinstance(units, list) or not units:
                raise ValueError("units must be a non-empty list")
            cursor = 0
            for unit in units:
                if not isinstance(unit, dict):
                    raise ValueError("unit must be an object")
                start = unit.get("start_index")
                count = unit.get("count")
                if (
                    isinstance(start, bool) or not isinstance(start, int)
                    or isinstance(count, bool) or not isinstance(count, int)
                    or start != cursor or count <= 0
                    or start + count > len(text_segs)
                ):
                    raise ValueError("plan has a gap, overlap, or invalid range")
                expected = source_fingerprint([
                    segment.source for segment in text_segs[start:start + count]
                ])
                if unit.get("source_fingerprint") != expected:
                    raise ValueError("source fingerprint mismatch")
                cursor += count
            if cursor != len(text_segs):
                raise ValueError("plan does not cover the chapter")
        except (TypeError, ValueError) as exc:
            store.log_event(
                "glossary_plan_invalid",
                chapter=chapter.index,
                error=str(exc),
            )
            raise RuntimeError(
                f"第 {chapter.index} 章术语计划与已保存正文不一致；"
                "为保护已有译文，已停止续跑。"
            ) from exc
        return units

    _batch_glossary_checkpoints = staticmethod(batch_glossary_checkpoints)
    _expected_chapter_glossary_plan = staticmethod(
        expected_chapter_glossary_plan
    )
    _chapter_glossary_windows = staticmethod(chapter_glossary_windows)
    _chapter_glossary_window_checkpoints = staticmethod(
        chapter_glossary_window_checkpoints
    )

    def _reconcile_chapter_glossary(
        self,
        glossary: GlossaryStore,
        store: RunStore,
        chapter,
        text_segs,
        units: list[dict],
    ) -> tuple[bool, bool]:
        return reconcile_chapter_glossary(
            glossary=glossary,
            store=store,
            chapter=chapter,
            text_segs=text_segs,
            units=units,
            extract_glossary=self._extract_glossary,
        )

    @staticmethod
    def _translation_runs(batch) -> list[tuple[int, list, bool]]:
        """按 target 是否已存在切连续段，已保存段永不再次送给译者。"""
        runs: list[tuple[int, list, bool]] = []
        start = 0
        completed = bool(batch[0].target and batch[0].target.strip())
        for index, segment in enumerate(batch[1:], start=1):
            current = bool(segment.target and segment.target.strip())
            if current == completed:
                continue
            runs.append((start, batch[start:index], completed))
            start = index
            completed = current
        runs.append((start, batch[start:], completed))
        return runs

    def _extract_glossary(
        self,
        glossary: GlossaryStore,
        store: RunStore,
        chapter: int,
        segments,
        *,
        phase: str,
        checkpoint: GlossaryCheckpoint,
    ) -> dict[str, int] | None:
        events = {
            "batch": "batch_glossary_extracted",
            "chapter_window": "chapter_glossary_window_extracted",
            "chapter": "chapter_glossary_extracted",
        }
        if phase not in events:
            raise ValueError(f"unsupported glossary extraction phase: {phase}")
        src_text = "\n".join(segment.source for segment in segments)
        tgt_text = "\n".join(segment.target or "" for segment in segments)
        try:
            summary = self.extractor.extract_and_store(
                glossary,
                src_text,
                tgt_text,
                chapter,
                checkpoint=checkpoint,
            )
        except GlossaryExtractionError as exc:
            store.log_event(
                "glossary_extraction_failed",
                phase=phase,
                chapter=chapter,
                start_index=checkpoint.start_index,
                count=checkpoint.count,
                fingerprint=checkpoint.fingerprint,
                plan_fingerprint=checkpoint.plan_fingerprint or None,
                error_kind=exc.kind,
            )
            return None
        except GlossaryPersistenceError as exc:
            cause = exc.__cause__ or exc
            store.log_event(
                "glossary_persistence_failed",
                phase=phase,
                chapter=chapter,
                start_index=checkpoint.start_index,
                count=checkpoint.count,
                fingerprint=checkpoint.fingerprint,
                plan_fingerprint=checkpoint.plan_fingerprint or None,
                summary=exc.summary,
                error=f"{type(cause).__name__}: {cause}"[:500],
            )
            raise
        except Exception as exc:
            store.log_event(
                "glossary_persistence_failed",
                phase=phase,
                chapter=chapter,
                start_index=checkpoint.start_index,
                count=checkpoint.count,
                fingerprint=checkpoint.fingerprint,
                plan_fingerprint=checkpoint.plan_fingerprint or None,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            raise

        event_data = {
            "chapter": chapter,
            "start_index": checkpoint.start_index,
            "count": checkpoint.count,
            "summary": summary,
            "fingerprint": checkpoint.fingerprint,
            "checkpoint_version": checkpoint.version,
            "completed": True,
            "generation_id": glossary.generation_id,
        }
        if checkpoint.plan_fingerprint:
            event_data["plan_fingerprint"] = checkpoint.plan_fingerprint
        store.log_event(
            events[phase],
            **event_data,
        )
        return summary

    def _refresh_changed_glossary(
        self,
        glossary: GlossaryStore,
        store: RunStore,
        chapter,
        text_segs,
        units: list[dict],
        changed_indices: set[int],
        complete: bool,
        term_snapshot: list,
    ) -> tuple[bool, list]:
        """审校自动修订 target 后刷新受影响 unit 与章级 checkpoint。"""
        chapter_index = chapter.index
        refreshed_units: list[int] = []
        for unit in units:
            start = unit["start_index"]
            end = start + unit["count"]
            if not any(start <= index < end for index in changed_indices):
                continue
            segments = text_segs[start:end]
            if not all(s.target and s.target.strip() for s in segments):
                complete = False
                continue
            checkpoint = GlossaryCheckpoint(
                scope="batch",
                chapter=chapter_index,
                start_index=start,
                count=len(segments),
                fingerprint=translation_fingerprint(segments),
            )
            if not glossary.checkpoint_matches(checkpoint):
                summary = self._extract_glossary(
                    glossary,
                    store,
                    chapter_index,
                    segments,
                    phase="batch",
                    checkpoint=checkpoint,
                )
                complete &= summary is not None
                if summary is not None:
                    term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
            refreshed_units.append(start)

        if all(s.target and s.target.strip() for s in text_segs):
            chapter_complete, chapter_changed = self._reconcile_chapter_glossary(
                glossary,
                store,
                chapter,
                text_segs,
                units,
            )
            complete = chapter_complete
            if chapter_changed:
                term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
        else:
            complete = False
        store.log_event(
            "glossary_post_review_refreshed",
            chapter=chapter_index,
            changed_indices=sorted(changed_indices),
            unit_starts=refreshed_units,
            completed=complete,
        )
        return complete, term_snapshot

    def _chapter_term_snapshot(self, glossary: GlossaryStore, text_segs) -> list:
        """返回当前章节要注入的术语快照；实时入库后可重新调用刷新。"""
        terms = glossary.all_terms()
        if self.config.pipeline.glossary_scope != "chapter":
            return terms
        src_text = "\n".join(s.source for s in text_segs)
        hit = {t.source for t in GlossaryStore.terms_in(terms, src_text)}
        return [t for t in terms
                if t.source in hit or (t.type == TYPE_PERSON and t.locked)]

    # ── 章末审校 + 严重项定向重译 ────────────────────────────────────────────
    _SEVERE_TYPES = ("missing", "mistranslation")

    def _review_chapter(self, text_segs, terms) -> list[dict]:
        """整章分块审校（章末统一做，不在批内阻塞翻译主路径）。

        块 = 连续段序列（约 3 倍翻译批大小，减少调用次数与重复注入的输入 token）；
        块内 reviewer 返回的 index 是块内下标，加块首段偏移映射回章内段号；
        越界 index 直接丢弃（模型幻觉防御）。
        """
        budget = self.config.segment.max_chars_per_batch * 3
        issues: list[dict] = []
        base = 0
        for chunk in self._pack_contiguous(text_segs, budget):
            srcs = [s.source for s in chunk]
            tgts = [s.target or "" for s in chunk]
            for it in self.reviewer.review(srcs, tgts, terms):
                idx = it.get("index")
                if (
                    isinstance(idx, int)
                    and not isinstance(idx, bool)
                    and 0 <= idx < len(chunk)
                ):
                    it["index"] = base + idx
                    issues.append(it)
            base += len(chunk)
        return issues

    @staticmethod
    def _pack_contiguous(segs, budget: int) -> list[list]:
        """按源文字符预算把段保序打包成若干连续块。"""
        chunks: list[list] = []
        cur: list = []
        size = 0
        for s in segs:
            if cur and size + len(s.source) > budget:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(s)
            size += len(s.source)
        if cur:
            chunks.append(cur)
        return chunks

    def _autofix_severe(self, text_segs, issues, terms, style,
                        book_synopsis: str = "", chapter_digest: str = "", *,
                        store: RunStore | None = None,
                        chapter_index: int | None = None,
                        mutable_indices: set[int] | None = None) -> None:
        """对审校严重项（漏译/误译）带审校意见定向重译，每段最多一次。

        采纳条件 = 重译非空且过长度校验：采纳则标点规范化后更新 seg.target 并标 fixed=True；
        不采纳保持 fixed=False 留人工。章末重译时原滚动上下文已失效，用该段前后各 2 段译文做局部上下文。
        """
        by_seg: dict[int, list[dict]] = {}
        for it in issues:
            index = it.get("index")
            if (
                it.get("type") in self._SEVERE_TYPES
                and isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(text_segs)
            ):
                by_seg.setdefault(index, []).append(it)
        for idx, seg_issues in sorted(by_seg.items()):
            if mutable_indices is not None and idx not in mutable_indices:
                for issue in seg_issues:
                    issue["fixed"] = False
                if store is not None:
                    store.log_event(
                        "autofix_skipped",
                        chapter=chapter_index,
                        index=idx,
                        source=text_segs[idx].source,
                        reason="saved_target_immutable",
                        issues=seg_issues,
                    )
                continue
            seg = text_segs[idx]
            before = "\n".join(text_segs[j].target or ""
                               for j in range(max(0, idx - 2), idx))
            after = "\n".join(text_segs[j].target or ""
                              for j in range(idx + 1, min(len(text_segs), idx + 3)))
            feedback = "；".join(
                f"{it.get('detail', '')}（建议：{it.get('suggestion', '')}）"
                for it in seg_issues)
            new_t = self.translator.retranslate_with_feedback(
                seg.source, feedback=feedback, glossary_terms=terms, style=style,
                context_before=before, context_after=after,
                book_synopsis=book_synopsis, chapter_digest=chapter_digest)
            if new_t and not checks.length_flags([seg.source], [new_t]):
                if self.config.punctuation_normalize:
                    new_t = normalize_zh(new_t)
                old_t = seg.target
                seg.target = new_t
                for it in seg_issues:
                    it["fixed"] = True
                if store is not None:
                    store.log_event(
                        "autofix_applied",
                        chapter=chapter_index,
                        index=idx,
                        source=seg.source,
                        before=old_t,
                        after=new_t,
                        issues=seg_issues,
                    )
            elif store is not None:
                store.log_event(
                    "autofix_rejected",
                    chapter=chapter_index,
                    index=idx,
                    source=seg.source,
                    before=seg.target,
                    proposed=new_t,
                    issues=seg_issues,
                )

    def _process_batch(self, batch, terms, ctx_text: str, style: str,
                       book_synopsis: str = "", chapter_digest: str = "") -> _BatchResult:
        """单个批次：整批翻译 → 润色 → 标点规范化。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        全书概览/本章梗概作为恒定前缀注入，让译者把握全局。
        LLM 审校不在批内做（移至章末统一做，见 _review_chapter，不阻塞翻译主路径）。
        """
        sources = [s.source for s in batch]
        targets = self.translator.translate_batch(
            sources, glossary_terms=terms, style=style, context=ctx_text,
            book_synopsis=book_synopsis, chapter_digest=chapter_digest)

        if self.config.pipeline.polish:
            polished = self.polisher.polish(targets, glossary_terms=terms, style=style)
            if len(polished) == len(targets):
                targets = polished

        if self.config.punctuation_normalize:
            targets = [normalize_zh(t) if t else t for t in targets]

        issues: list[dict] = []
        for flag in checks.length_flags(sources, targets):
            issues.append({
                "index": flag.index,
                "type": "length",
                "stage": "length",
                "reason": flag.reason,
                "ratio": flag.ratio,
                "detail": f"译文长度异常：{flag.reason}，译文/原文字符比 {flag.ratio:.2f}",
            })

        return _BatchResult(
            targets=targets,
            issues=issues,
        )

    # ── 可选步骤 / 连续全流程 ────────────────────────────────────────────────
    ALL_STEPS = ("translate", "qa", "report", "assemble")

    def run_steps(self, input_path: str, steps, *,
                  progress: Optional[ProgressFn] = None,
                  out_format: str = "epub", out_path: str | None = None) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        from ..agents.consistency import ConsistencyChecker
        from ..assemble.writer import assemble
        from ..assemble.report import build_report

        steps = set(steps)
        run_steps_input = sorted(steps)

        if "translate" in steps:
            store = self.run(input_path, progress=progress)
        else:
            store = self.prepare(input_path)
            m = store.load_manifest()
            self._apply_language(m.get("source_lang") or self.config.source_lang)
        store.log_event("run_steps_started", steps=run_steps_input, input_path=input_path)

        glossary = store.open_glossary()
        qa_issues: list[dict] = []
        report: dict[str, Any] | None = None
        try:
            if "qa" in steps:
                qa_issues = ConsistencyChecker(self.client, self.config).check(store, glossary)
                store.log_event(
                    "consistency_qa_finished",
                    issue_count=len(qa_issues),
                    issues=qa_issues,
                )

            if "report" in steps:
                report = build_report(store, glossary)
                report["consistency_issues"] = qa_issues
                store.save_report(report)
                store.log_event("report_saved", path=store.report_path)
        finally:
            glossary.close()

        out = None
        if "assemble" in steps:
            out = assemble(store, input_path, out_path=out_path, out_format=out_format)
            store.log_event("assembled", output=out, out_format=out_format)

        store.log_event(
            "run_steps_finished",
            steps=run_steps_input,
            output=out,
            qa_issue_count=len(qa_issues),
        )
        return {"store": store, "output": out, "report": report,
                "qa_issues": qa_issues}

    def run_all(self, input_path: str, *, progress: Optional[ProgressFn] = None,
                out_format: str = "epub", out_path: str | None = None,
                do_qa: bool | None = None) -> dict[str, Any]:
        """翻译 → 一致性 QA → 报告 → 回填 EPUB，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if do_qa if do_qa is not None else self.config.pipeline.consistency_qa:
            steps.add("qa")
        return self.run_steps(input_path, steps, progress=progress,
                              out_format=out_format, out_path=out_path)
