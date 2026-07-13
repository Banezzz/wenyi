"""SQLite 术语库 + 翻译记忆库。

核心数据表：
- glossary：专有名词对照表（source 唯一）。冲突检测：同 source 出现不同 target 时，
  若现有条目已锁定/高置信度则保留并记入 term_conflicts，否则更新。
- term_conflicts：待裁决的译法冲突日志，供人工复核。
- translation_memory：句群级译文对，供一致性参考与重译复用。
- glossary_extraction_checkpoints：术语抽取完成点，和批量术语写入原子提交。
- store_metadata：术语库世代标识，防止旧 checkpoint 被错误复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

# 术语类型
TYPE_PERSON = "人物"
TYPE_PLACE = "地名"
TYPE_ORG = "组织"
TYPE_TERM = "术语"
TYPE_SKILL = "招式"
TYPE_APPELLATION = "称谓"
TYPE_HONORIFIC = "敬称"
TYPE_SPEECH = "口癖"
TYPE_FIXED_EXPR = "固定表达"
TYPE_ONOMATOPOEIA = "拟声词"

_SOURCE_ONLY_TYPES = {TYPE_APPELLATION, TYPE_HONORIFIC, TYPE_SPEECH, TYPE_FIXED_EXPR}

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_HAN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LATIN_WORD_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass
class GlossaryTerm:
    source: str
    target: str
    reading: str = ""
    type: str = TYPE_TERM
    gender: str = ""
    aliases: list[str] = field(default_factory=list)
    first_chapter: Optional[int] = None
    note: str = ""
    confidence: str = "medium"
    locked: bool = False
    status: str = "ok"

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        type_override: str | None = None,
        default_type: str = TYPE_TERM,
        confidence: str = "medium",
        first_chapter: int | None = None,
    ) -> Optional["GlossaryTerm"]:
        """Normalize an untrusted mapping into a term.

        Required values are never coerced: model output with a list/dict in
        ``source`` or ``target`` is discarded. Optional scalar values follow
        the same type boundary but degrade to their defaults instead of making
        an otherwise valid term unusable.
        """
        if not isinstance(data, Mapping):
            return None

        source = _nonempty_string(data.get("source"))
        target = _nonempty_string(data.get("target"))
        if source is None or target is None:
            return None

        fallback_type = _nonempty_string(default_type) or TYPE_TERM
        if type_override is None:
            term_type = _nonempty_string(data.get("type")) or fallback_type
        else:
            term_type = _nonempty_string(type_override) or fallback_type

        gender = _optional_string(data.get("gender"))
        if gender.casefold() == "unknown" or gender == "未知":
            gender = ""

        aliases: list[str] = []
        seen_aliases: set[str] = set()
        raw_aliases = data.get("aliases")
        if isinstance(raw_aliases, list):
            for value in raw_aliases:
                alias = _nonempty_string(value)
                if alias is not None and alias not in seen_aliases:
                    aliases.append(alias)
                    seen_aliases.add(alias)

        normalized_confidence = _nonempty_string(confidence) or "medium"
        normalized_chapter = _normalize_optional_int(first_chapter)
        return cls(
            source=source,
            target=target,
            reading=_optional_string(data.get("reading")),
            type=term_type,
            gender=gender,
            aliases=aliases,
            first_chapter=normalized_chapter,
            note=_optional_string(data.get("note")),
            confidence=normalized_confidence,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GlossaryTerm":
        raw_aliases = _row_value(row, "aliases", "[]")
        if isinstance(raw_aliases, str):
            try:
                raw_aliases = json.loads(raw_aliases)
            except (json.JSONDecodeError, TypeError):
                raw_aliases = []

        confidence = _row_value(row, "confidence", "medium")
        if not isinstance(confidence, str):
            confidence = "medium"
        first_chapter = _normalize_optional_int(
            _row_value(row, "first_chapter", None)
        )
        term = cls.from_mapping(
            {
                "source": _row_value(row, "source", None),
                "target": _row_value(row, "target", None),
                "reading": _row_value(row, "reading", ""),
                "type": _row_value(row, "type", TYPE_TERM),
                "gender": _row_value(row, "gender", ""),
                "aliases": raw_aliases,
                "note": _row_value(row, "note", ""),
            },
            confidence=confidence,
            first_chapter=first_chapter,
        )
        if term is None:
            raise ValueError("glossary row has invalid source or target")
        locked = _row_value(row, "locked", 0)
        term.locked = bool(locked) if isinstance(locked, (bool, int)) else False
        term.status = _optional_string(_row_value(row, "status", "ok")) or "ok"
        return term


@dataclass(frozen=True)
class GlossaryCheckpoint:
    scope: str
    chapter: int
    start_index: int
    count: int
    fingerprint: str
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str):
            raise TypeError("checkpoint.scope must be a string")
        if self.scope not in {"batch", "chapter"}:
            raise ValueError("checkpoint scope must be 'batch' or 'chapter'")
        _validate_int("checkpoint.chapter", self.chapter, minimum=0)
        _validate_int("checkpoint.start_index", self.start_index, minimum=0)
        _validate_int("checkpoint.count", self.count, minimum=0)
        _validate_int("checkpoint.version", self.version, minimum=1)
        if _nonempty_string(self.fingerprint) is None:
            raise ValueError("checkpoint.fingerprint must be a non-empty string")


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row_value(row: sqlite3.Row, key: str, default: Any) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _validate_int(name: str, value: Any, *, minimum: int | None = None) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS glossary (
    source        TEXT PRIMARY KEY,
    target        TEXT NOT NULL,
    reading       TEXT,
    type          TEXT,
    gender        TEXT,
    aliases       TEXT,
    first_chapter INTEGER,
    note          TEXT,
    confidence    TEXT DEFAULT 'medium',
    locked        INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'ok',
    updated_at    REAL
);
CREATE TABLE IF NOT EXISTS term_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    existing_target TEXT,
    proposed_target TEXT,
    chapter         INTEGER,
    note            TEXT,
    resolved        INTEGER DEFAULT 0,
    created_at      REAL
);
CREATE TABLE IF NOT EXISTS translation_memory (
    source_hash TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    chapter     INTEGER,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS store_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary_extraction_checkpoints (
    scope         TEXT NOT NULL CHECK(scope IN ('batch', 'chapter')),
    chapter       INTEGER NOT NULL,
    start_index   INTEGER NOT NULL,
    count         INTEGER NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    generation_id TEXT NOT NULL,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (scope, chapter, start_index)
);
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class GlossaryStoreIdentityError(RuntimeError):
    """The database does not match the run manifest that owns it."""


class GlossaryStore:
    def __init__(
        self,
        db_path: str,
        *,
        create: bool = True,
        expected_generation_id: str | None = None,
    ):
        self.db_path = db_path
        if expected_generation_id is not None and _nonempty_string(
            expected_generation_id
        ) is None:
            raise ValueError("expected_generation_id must be a non-empty string")
        if not create and not os.path.isfile(db_path):
            raise FileNotFoundError(f"glossary database is missing: {db_path}")
        if create:
            self.conn = sqlite3.connect(db_path)
        else:
            # mode=rw closes the delete-between-check-and-connect race: SQLite must
            # never create an empty replacement for an existing run.
            uri = f"file:{os.path.abspath(db_path)}?mode=rw"
            self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        # 并发写等待，避免 Web 编辑与翻译 worker 同写时报 "database is locked"
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        if expected_generation_id is not None:
            try:
                row = self.conn.execute(
                    "SELECT value FROM store_metadata WHERE key='generation_id'"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                self.conn.close()
                raise GlossaryStoreIdentityError(
                    "glossary database has no generation metadata"
                ) from exc
            if row is None or row["value"] != expected_generation_id:
                actual = row["value"] if row is not None else "missing"
                self.conn.close()
                raise GlossaryStoreIdentityError(
                    "glossary database generation mismatch: "
                    f"expected {expected_generation_id}, got {actual}"
                )
        self.conn.executescript(_SCHEMA)
        candidate_generation_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT OR IGNORE INTO store_metadata (key, value)
               VALUES ('generation_id', ?)""",
            (candidate_generation_id,),
        )
        row = self.conn.execute(
            "SELECT value FROM store_metadata WHERE key='generation_id'"
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by INSERT above
            raise RuntimeError("failed to initialize glossary generation_id")
        self.generation_id = row["value"]
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── 术语 ──────────────────────────────────────────────────────────────
    def get_term(self, source: str) -> Optional[GlossaryTerm]:
        row = self.conn.execute(
            "SELECT * FROM glossary WHERE source = ?", (source,)
        ).fetchone()
        return GlossaryTerm.from_row(row) if row else None

    def upsert_term(self, term: GlossaryTerm, chapter: Optional[int] = None) -> str:
        """插入或更新术语，返回 'inserted'|'updated'|'unchanged'|'conflict'。

        冲突规则：同 source 已存在且 target 不同时——
          现有条目 locked 或置信度更高 → 保留现有，记冲突，返回 'conflict'；
          否则用新条目覆盖，返回 'updated'。
        """
        self._validate_term_for_storage(term)
        self._validate_chapter(chapter, allow_none=True)
        try:
            result = self._upsert_term(term, chapter)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def upsert_terms(
        self,
        terms: Iterable[GlossaryTerm],
        *,
        chapter: int,
        checkpoint: GlossaryCheckpoint | None,
    ) -> dict[str, int]:
        """Atomically store a glossary extraction result and its checkpoint."""
        materialized = list(terms)
        self._validate_chapter(chapter, allow_none=False)
        if checkpoint is not None:
            self._validate_checkpoint(checkpoint)
            if checkpoint.chapter != chapter:
                raise ValueError("checkpoint.chapter must match chapter")
        for term in materialized:
            self._validate_term_for_storage(term)

        summary = {"inserted": 0, "updated": 0, "conflict": 0, "unchanged": 0}
        try:
            self.conn.execute("BEGIN")
            for term in materialized:
                result = self._upsert_term(term, chapter)
                summary[result] += 1
            if checkpoint is not None:
                self.record_checkpoint(checkpoint, commit=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return summary

    def _upsert_term(self, term: GlossaryTerm, chapter: Optional[int]) -> str:
        existing = self.get_term(term.source)
        now = time.time()
        if existing is None:
            self.conn.execute(
                """INSERT INTO glossary
                   (source,target,reading,type,gender,aliases,first_chapter,note,
                    confidence,locked,status,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    term.source, term.target, term.reading, term.type, term.gender,
                    json.dumps(term.aliases, ensure_ascii=False),
                    term.first_chapter if term.first_chapter is not None else chapter,
                    term.note, term.confidence, int(term.locked), term.status, now,
                ),
            )
            return "inserted"

        if existing.target == term.target:
            # 合并别名 / 补全字段，不算冲突
            merged_aliases = list(existing.aliases)
            seen_aliases = set(merged_aliases)
            for alias in term.aliases:
                if alias not in seen_aliases:
                    merged_aliases.append(alias)
                    seen_aliases.add(alias)
            self.conn.execute(
                """UPDATE glossary SET reading=COALESCE(NULLIF(?,''),reading),
                   gender=COALESCE(NULLIF(?,''),gender), aliases=?, note=COALESCE(NULLIF(?,''),note),
                   updated_at=? WHERE source=?""",
                (term.reading, term.gender, json.dumps(merged_aliases, ensure_ascii=False),
                 term.note, now, term.source),
            )
            return "unchanged"

        # target 不同 → 冲突判定
        existing_priority = (existing.locked, CONFIDENCE_ORDER.get(existing.confidence, 1))
        new_priority = (term.locked, CONFIDENCE_ORDER.get(term.confidence, 1))
        self._log_conflict(term.source, existing.target, term.target, chapter)
        if existing_priority >= new_priority:
            self.conn.execute(
                "UPDATE glossary SET status='conflict', updated_at=? WHERE source=?",
                (now, term.source),
            )
            return "conflict"
        else:
            self.conn.execute(
                """UPDATE glossary SET target=?, reading=COALESCE(NULLIF(?,''),reading),
                   gender=COALESCE(NULLIF(?,''),gender), confidence=?, status='conflict',
                   updated_at=? WHERE source=?""",
                (term.target, term.reading, term.gender, term.confidence, now, term.source),
            )
            return "updated"

    def _log_conflict(
        self,
        source: str,
        existing_target: str,
        proposed_target: str,
        chapter: Optional[int],
    ) -> None:
        self.conn.execute(
            """INSERT INTO term_conflicts
               (source,existing_target,proposed_target,chapter,created_at)
               VALUES (?,?,?,?,?)""",
            (source, existing_target, proposed_target, chapter, time.time()),
        )

    @staticmethod
    def _validate_term_for_storage(term: GlossaryTerm) -> None:
        if not isinstance(term, GlossaryTerm):
            raise TypeError("term must be a GlossaryTerm")
        for name in (
            "source", "target", "reading", "type", "gender", "note",
            "confidence", "status",
        ):
            value = getattr(term, name)
            if not isinstance(value, str):
                raise TypeError(f"term.{name} must be a string")
        if not term.source.strip() or not term.target.strip():
            raise ValueError("term.source and term.target must be non-empty strings")
        if not isinstance(term.aliases, list):
            raise TypeError("term.aliases must be a list of strings")
        if any(not isinstance(alias, str) for alias in term.aliases):
            raise TypeError("term.aliases must contain only strings")
        if term.first_chapter is not None:
            _validate_int("term.first_chapter", term.first_chapter, minimum=0)
        if not isinstance(term.locked, bool):
            raise TypeError("term.locked must be a bool")

    @staticmethod
    def _validate_chapter(chapter: Any, *, allow_none: bool) -> None:
        if chapter is None and allow_none:
            return
        _validate_int("chapter", chapter, minimum=0)

    @staticmethod
    def _validate_checkpoint(checkpoint: GlossaryCheckpoint) -> None:
        if not isinstance(checkpoint, GlossaryCheckpoint):
            raise TypeError("checkpoint must be a GlossaryCheckpoint")

    def checkpoint_matches(self, checkpoint: GlossaryCheckpoint) -> bool:
        self._validate_checkpoint(checkpoint)
        row = self.conn.execute(
            """SELECT count, fingerprint, version, generation_id
               FROM glossary_extraction_checkpoints
               WHERE scope=? AND chapter=? AND start_index=?""",
            (checkpoint.scope, checkpoint.chapter, checkpoint.start_index),
        ).fetchone()
        return bool(
            row
            and row["count"] == checkpoint.count
            and row["fingerprint"] == checkpoint.fingerprint
            and row["version"] == checkpoint.version
            and row["generation_id"] == self.generation_id
        )

    def record_checkpoint(
        self,
        checkpoint: GlossaryCheckpoint,
        *,
        commit: bool = True,
    ) -> None:
        self._validate_checkpoint(checkpoint)
        try:
            self.conn.execute(
                """INSERT INTO glossary_extraction_checkpoints
                   (scope,chapter,start_index,count,fingerprint,version,
                    generation_id,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope,chapter,start_index) DO UPDATE SET
                       count=excluded.count,
                       fingerprint=excluded.fingerprint,
                       version=excluded.version,
                       generation_id=excluded.generation_id,
                       updated_at=excluded.updated_at""",
                (
                    checkpoint.scope,
                    checkpoint.chapter,
                    checkpoint.start_index,
                    checkpoint.count,
                    checkpoint.fingerprint,
                    checkpoint.version,
                    self.generation_id,
                    time.time(),
                ),
            )
            if commit:
                self.conn.commit()
        except Exception:
            if commit:
                self.conn.rollback()
            raise

    def delete_term(self, source: str) -> bool:
        """删除一个术语条目（前端编辑用）。返回是否确有删除。"""
        cur = self.conn.execute("DELETE FROM glossary WHERE source = ?", (source,))
        self.conn.commit()
        return cur.rowcount > 0

    def lock_term(self, source: str, target: Optional[str] = None) -> None:
        if target is not None:
            self.conn.execute(
                "UPDATE glossary SET target=?, locked=1, confidence='high', status='ok' WHERE source=?",
                (target, source),
            )
        else:
            self.conn.execute(
                "UPDATE glossary SET locked=1, confidence='high', status='ok' WHERE source=?",
                (source,),
            )
        self.conn.commit()

    def all_terms(self) -> list[GlossaryTerm]:
        rows = self.conn.execute(
            "SELECT * FROM glossary ORDER BY type, source"
        ).fetchall()
        return [GlossaryTerm.from_row(r) for r in rows]

    @staticmethod
    def _contains_key(text: str, key: str) -> bool:
        """术语命中：避免短词在普通词内部误命中，同时保留日文助词场景。"""
        if not key:
            return False
        if _LATIN_WORD_RE.fullmatch(key):
            return re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])",
                text,
            ) is not None
        if len(key) == 1 and _HAN_RE.fullmatch(key):
            return re.search(
                rf"(?<![\u3400-\u9fff\uf900-\ufaff]){re.escape(key)}(?![\u3400-\u9fff\uf900-\ufaff])",
                text,
            ) is not None
        return key in text

    @staticmethod
    def terms_in(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        """从给定术语列表里筛出 source 或任一别名在 text 中出现的项。

        与 terms_in_text 同义，但接受预取的术语快照，避免逐批重复查库（章内术语表不变）。
        """
        out: list[GlossaryTerm] = []
        for term in terms:
            # 称谓/口癖/固定表达是带语气或场景的派生写法，不能因为 alias
            # 命中裸名就把派生译法注入到普通称呼处。
            keys = (
                [term.source]
                if term.type in _SOURCE_ONLY_TYPES
                else [term.source] + term.aliases
            )
            if any(GlossaryStore._contains_key(text, k) for k in keys):
                out.append(term)
        return out

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        """返回 source 或任一别名在 text 中出现的术语（注入翻译 prompt 用）。"""
        return self.terms_in(self.all_terms(), text)

    def mark_conflicts_resolved(self, source: str) -> None:
        self.conn.execute(
            "UPDATE term_conflicts SET resolved=1 WHERE source=?", (source,)
        )
        self.conn.commit()

    def open_conflicts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM term_conflicts WHERE resolved=0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def low_confidence_terms(self) -> list[GlossaryTerm]:
        rows = self.conn.execute(
            "SELECT * FROM glossary WHERE confidence='low' OR status='conflict' ORDER BY source"
        ).fetchall()
        return [GlossaryTerm.from_row(r) for r in rows]

    # ── 翻译记忆库 ──────────────────────────────────────────────────────
    def add_tm(self, source_text: str, target_text: str, chapter: Optional[int] = None) -> None:
        self.conn.execute(
            """INSERT INTO translation_memory (source_hash,source_text,target_text,chapter,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(source_hash) DO UPDATE SET target_text=excluded.target_text,
                   chapter=excluded.chapter, updated_at=excluded.updated_at""",
            (_hash(source_text), source_text, target_text, chapter, time.time()),
        )
        self.conn.commit()

    def tm_lookup(self, source_text: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT target_text FROM translation_memory WHERE source_hash=?",
            (_hash(source_text),),
        ).fetchone()
        return row["target_text"] if row else None

    def stats(self) -> dict[str, int]:
        g = self.conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(*) FROM term_conflicts WHERE resolved=0").fetchone()[0]
        t = self.conn.execute("SELECT COUNT(*) FROM translation_memory").fetchone()[0]
        return {"terms": g, "open_conflicts": c, "tm_entries": t}
