"""运行态持久化：支持断点续跑。

目录结构（state_dir/<book-slug>/）：
  manifest.json     书籍元信息 + 各章状态
  chapters/ch{n}.json  各章（含 source/target 的 Segment）
  context.json      滚动上下文（梗概 + 前文尾段）
  analysis.json     全局分析结果
  glossary.db       术语库 + 翻译记忆库
  report.json       QA 报告
  events.jsonl      追加式行为 / 改写 / 翻译结果日志
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from ..ingest.models import Chapter, Document

STATUS_PENDING = "pending"
STATUS_DONE = "done"
GLOSSARY_PENDING = "pending"
GLOSSARY_DONE = "done"


def _frame(hasher, value: str) -> None:
    data = value.encode("utf-8")
    hasher.update(len(data).to_bytes(8, "big"))
    hasher.update(data)


def source_fingerprint(sources: list[str]) -> str:
    """按长度分帧，避免不同字符串分组产生相同拼接内容。"""
    hasher = hashlib.sha256()
    for source in sources:
        if not isinstance(source, str):
            raise TypeError("source fingerprint values must be strings")
        _frame(hasher, source)
    return hasher.hexdigest()


def translation_fingerprint(segments: list[Any]) -> str:
    """计算有序 source/target 对的稳定指纹。"""
    hasher = hashlib.sha256()
    for segment in segments:
        if isinstance(segment, dict):
            source = segment.get("source")
            target = segment.get("target")
        else:
            source = getattr(segment, "source", None)
            target = getattr(segment, "target", None)
        if not isinstance(source, str) or target is not None and not isinstance(target, str):
            raise TypeError("translation fingerprint requires string source/target")
        _frame(hasher, source)
        _frame(hasher, target or "")
    return hasher.hexdigest()


def _event_batch_key(row: dict[str, Any]) -> tuple[int, int, int] | None:
    values = (row.get("chapter"), row.get("start_index"), row.get("count"))
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return None
    chapter, start, count = values
    if chapter < 0 or start < 0 or count <= 0:
        return None
    return chapter, start, count


def slugify(name: str) -> str:
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    return s or "book"


class RunStore:
    def __init__(self, run_dir: str, *, create: bool = True):
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        os.makedirs(self.chapters_dir, exist_ok=True)

    # ── 路径 ──────────────────────────────────────────────────────────────
    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def context_path(self) -> str:
        return os.path.join(self.run_dir, "context.json")

    @property
    def analysis_path(self) -> str:
        return os.path.join(self.run_dir, "analysis.json")

    @property
    def glossary_path(self) -> str:
        return os.path.join(self.run_dir, "glossary.db")

    @property
    def report_path(self) -> str:
        return os.path.join(self.run_dir, "report.json")

    @property
    def event_log_path(self) -> str:
        return os.path.join(self.run_dir, "events.jsonl")

    def chapter_path(self, ci: int) -> str:
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    # ── 通用 JSON ─────────────────────────────────────────────────────────
    @staticmethod
    def _write_json(path: str, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半中断

    @staticmethod
    def _read_json(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self) -> bool:
        return os.path.isfile(self.manifest_path)

    def orphan_artifacts(self) -> list[str]:
        """List state artifacts that cannot be owned without a manifest."""
        if self.exists() or not os.path.isdir(self.run_dir):
            return []
        artifacts: list[str] = []
        for entry in os.scandir(self.run_dir):
            if entry.name == "chapters" and entry.is_dir(follow_symlinks=False):
                with os.scandir(entry.path) as children:
                    artifacts.extend(child.path for child in children)
                continue
            artifacts.append(entry.path)
        return artifacts

    # ── manifest ──────────────────────────────────────────────────────────
    def init_from_document(
        self,
        doc: Document,
        *,
        glossary_generation_id: str | None = None,
    ) -> dict:
        manifest = {
            "state_format_version": 1,
            "title": doc.title,
            "fmt": doc.fmt,
            "source_path": doc.source_path,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": doc.meta,
            "analysis_glossary_status": GLOSSARY_PENDING,
            "titles_status": STATUS_PENDING,
            "chapters": [
                {"index": c.index, "title": c.title,
                 "href": c.href, "status": STATUS_PENDING,
                 "glossary_status": GLOSSARY_PENDING}
                for c in doc.chapters
            ],
        }
        if glossary_generation_id is not None:
            if not isinstance(glossary_generation_id, str) or not glossary_generation_id:
                raise ValueError("glossary_generation_id must be a non-empty string")
            manifest["glossary_generation_id"] = glossary_generation_id
        self.save_manifest(manifest)
        for c in doc.chapters:
            self.save_chapter(c)
        return manifest

    def save_manifest(self, manifest: dict) -> None:
        self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        return self._read_json(self.manifest_path)

    def set_chapter_status(self, ci: int, status: str) -> None:
        manifest = self.load_manifest()
        for c in manifest["chapters"]:
            if c["index"] == ci:
                c["status"] = status
                break
        self.save_manifest(manifest)

    def pending_chapters(self) -> list[int]:
        manifest = self.load_manifest()
        return [c["index"] for c in manifest["chapters"] if c["status"] != STATUS_DONE]

    def pending_work_chapters(self) -> list[int]:
        """返回仍需翻译或仍有术语抽取待重试的章节。"""
        manifest = self.load_manifest()
        return [
            c["index"]
            for c in manifest["chapters"]
            if c["status"] != STATUS_DONE
            or c.get("glossary_status") == GLOSSARY_PENDING
        ]

    def set_chapter_glossary_status(self, ci: int, status: str) -> None:
        if status not in {GLOSSARY_PENDING, GLOSSARY_DONE}:
            raise ValueError(f"invalid glossary status: {status}")
        manifest = self.load_manifest()
        for chapter in manifest["chapters"]:
            if chapter["index"] == ci:
                chapter["glossary_status"] = status
                chapter.pop("glossary_legacy", None)
                break
        else:
            raise KeyError(f"chapter not found: {ci}")
        self.save_manifest(manifest)

    def set_analysis_glossary_status(self, status: str) -> None:
        if status not in {GLOSSARY_PENDING, GLOSSARY_DONE}:
            raise ValueError(f"invalid analysis glossary status: {status}")
        manifest = self.load_manifest()
        manifest["analysis_glossary_status"] = status
        self.save_manifest(manifest)

    def set_titles_status(self, status: str) -> None:
        if status not in {STATUS_PENDING, STATUS_DONE}:
            raise ValueError(f"invalid titles status: {status}")
        if status == STATUS_PENDING:
            self.invalidate_titles()
            return
        manifest = self.load_manifest()
        manifest["titles_status"] = status
        self.save_manifest(manifest)

    def invalidate_titles(self) -> None:
        """Mark titles stale and remove translated fields from exported state."""
        manifest = self.load_manifest()
        manifest["titles_status"] = STATUS_PENDING
        manifest.pop("title_translated", None)
        for chapter in manifest.get("chapters", []):
            if isinstance(chapter, dict):
                chapter.pop("title_translated", None)
        meta = manifest.get("meta")
        if isinstance(meta, dict):
            toc_entries = meta.get("toc_entries")
            if isinstance(toc_entries, list):
                for entry in toc_entries:
                    if isinstance(entry, dict):
                        entry.pop("title_translated", None)
        self.save_manifest(manifest)

    def bind_glossary_generation(self, generation_id: str) -> None:
        """Bind a legacy manifest once, then reject any database replacement."""
        if not isinstance(generation_id, str) or not generation_id:
            raise ValueError("generation_id must be a non-empty string")
        manifest = self.load_manifest()
        expected = manifest.get("glossary_generation_id")
        if expected is not None and expected != generation_id:
            raise RuntimeError(
                "manifest glossary generation does not match the database"
            )
        if expected is None:
            if not self._is_legacy_glossary_state(manifest):
                from ..glossary.store import GlossaryStoreIdentityError

                raise GlossaryStoreIdentityError(
                    "new-format manifest is missing glossary_generation_id"
                )
            for entry in manifest.get("chapters", []):
                entry["glossary_legacy"] = True
            manifest["state_format_version"] = 1
            manifest["glossary_generation_id"] = generation_id
            self.save_manifest(manifest)

    def _is_legacy_glossary_state(self, manifest: dict) -> bool:
        """Legacy means every marker introduced by the checkpoint format is absent."""
        if any(
            key in manifest
            for key in (
                "state_format_version",
                "analysis_glossary_status",
                "titles_status",
                "glossary_generation_id",
            )
        ):
            return False
        for entry in manifest.get("chapters", []):
            if (
                not isinstance(entry, dict)
                or "glossary_status" in entry
                or "glossary_legacy" in entry
            ):
                return False
            try:
                chapter = self.load_chapter(entry["index"])
            except (KeyError, OSError, ValueError, TypeError):
                return False
            if "glossary_plan" in chapter.meta:
                return False
        return True

    def open_glossary(self, *, create: bool = False):
        """Open this run's database without ever silently replacing it."""
        from ..glossary.store import GlossaryStore

        manifest = self.load_manifest()
        expected = manifest.get("glossary_generation_id")
        if expected is None and not self._is_legacy_glossary_state(manifest):
            from ..glossary.store import GlossaryStoreIdentityError

            raise GlossaryStoreIdentityError(
                "new-format manifest is missing glossary_generation_id"
            )
        glossary = GlossaryStore(
            self.glossary_path,
            create=create,
            expected_generation_id=expected,
        )
        try:
            self.bind_glossary_generation(glossary.generation_id)
        except Exception:
            glossary.close()
            raise
        return glossary

    # ── 章 ────────────────────────────────────────────────────────────────
    def save_chapter(self, chapter: Chapter) -> None:
        self._write_json(self.chapter_path(chapter.index), chapter.to_dict())

    def load_chapter(self, ci: int) -> Chapter:
        return Chapter.from_dict(self._read_json(self.chapter_path(ci)))

    # ── 上下文 / 分析 / 报告 ──────────────────────────────────────────────
    def save_context(self, data: dict) -> None:
        self._write_json(self.context_path, data)

    def load_context(self) -> dict | None:
        return self._read_json(self.context_path) if os.path.isfile(self.context_path) else None

    def save_analysis(self, data: dict) -> None:
        self._write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        return self._read_json(self.analysis_path) if os.path.isfile(self.analysis_path) else None

    def save_report(self, data: dict) -> None:
        self._write_json(self.report_path, data)

    # ── 追加式事件日志 ────────────────────────────────────────────────────
    def log_event(self, event: str, **data: Any) -> None:
        """追加一条 JSONL 事件，用于翻译行为、改写前后和产物对账。"""
        self.ensure_dirs()
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with open(self.event_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def legacy_batch_glossary_evidence(
        self,
    ) -> dict[tuple[int, int, int], str]:
        """读取可安全晋升的旧版批次事件。

        旧版零计数事件可能是 `_ask_json(default=[])` 吞掉的模型失败，不能信任。
        非零事件只有在同 key 的译文内容可重建指纹时才返回。
        """
        if not os.path.isfile(self.event_log_path):
            return {}
        translated: dict[tuple[int, int, int], str] = {}
        completed: dict[tuple[int, int, int], str] = {}
        count_keys = ("inserted", "updated", "conflict", "unchanged")
        with open(self.event_log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                key = _event_batch_key(row)
                if key is None:
                    continue
                if row.get("event") == "batch_translated":
                    segments = row.get("segments")
                    if not isinstance(segments, list) or len(segments) != key[2]:
                        continue
                    if any(
                        not isinstance(segment, dict)
                        or segment.get("index") != key[1] + offset
                        for offset, segment in enumerate(segments)
                    ):
                        continue
                    try:
                        translated[key] = translation_fingerprint(segments)
                    except TypeError:
                        continue
                    continue
                if row.get("event") != "batch_glossary_extracted":
                    continue
                if row.get("checkpoint_version") is not None:
                    continue
                summary = row.get("summary")
                if not isinstance(summary, dict):
                    continue
                values = [summary.get(name) for name in count_keys]
                if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
                    continue
                if sum(values) <= 0:
                    continue
                fingerprint = translated.get(key)
                if fingerprint:
                    completed[key] = fingerprint
        return completed
