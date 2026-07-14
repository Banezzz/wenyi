"""术语抽取 Agent（廉价档）+ 原子入库。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..agents import prompts
from ..agents.base import Agent
from .store import GlossaryCheckpoint, GlossaryStore, GlossaryTerm


class GlossaryExtractionError(RuntimeError):
    """模型调用或响应契约失败；不代表 SQLite 持久化失败。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class GlossaryPersistenceError(RuntimeError):
    """术语持久化失败；事务已回滚，调用方必须停止并上报。"""

    def __init__(self, summary: dict[str, int]):
        super().__init__("glossary persistence failed")
        self.summary = summary


@dataclass(frozen=True)
class _ParsedTerms:
    terms: list[GlossaryTerm]
    stats: dict[str, int]


GLOSSARY_EXTRACTOR_MAX_PROMPT_CHARS = 30_000


def _serialized_prompt_chars(system: str, user: str) -> int:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return len(json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _was_normalized(raw: dict[str, Any], term: GlossaryTerm) -> bool:
    """只统计响应中实际出现且被改写的字段，不把缺省字段算作归一化。"""
    for name in ("source", "target", "reading", "type", "gender", "aliases", "note"):
        if name in raw and raw[name] != getattr(term, name):
            return True
    return False


class GlossaryExtractor(Agent):
    def _ask_strict_json(self, system: str, user: str) -> Any:
        text = self.client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tier="fast",
            json_mode=True,
        )
        return json.loads(
            (text or "").strip(),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )

    def _render_bounded_prompt(
        self,
        source_text: str,
        target_text: str,
        existing: list[GlossaryTerm],
    ) -> tuple[str, str, list[GlossaryTerm], int, int]:
        """Render the exact request, dropping only whole reference entries."""
        system = prompts.render("glossary_extractor_system", src=self.src, tgt=self.tgt)

        def render(selected: list[GlossaryTerm]) -> tuple[str, str, int]:
            rendered = prompts.render_glossary(selected)
            user = prompts.render(
                "glossary_extractor_user",
                src=self.src,
                tgt=self.tgt,
                glossary=rendered,
                source=source_text,
                target=target_text,
            )
            return rendered, user, _serialized_prompt_chars(system, user)

        rendered_glossary, user, prompt_chars = render([])
        if prompt_chars > GLOSSARY_EXTRACTOR_MAX_PROMPT_CHARS:
            raise GlossaryExtractionError("prompt_too_large")

        selected: list[GlossaryTerm] = []
        for term in existing:
            candidate = [*selected, term]
            candidate_glossary, candidate_user, candidate_chars = render(candidate)
            if candidate_chars > GLOSSARY_EXTRACTOR_MAX_PROMPT_CHARS:
                continue
            selected = candidate
            rendered_glossary = candidate_glossary
            user = candidate_user
            prompt_chars = candidate_chars

        return system, user, selected, len(rendered_glossary), prompt_chars

    def _extract_with_stats(
        self,
        source_text: str,
        target_text: str,
        existing: list[GlossaryTerm],
        *,
        reference_total: int | None = None,
    ) -> _ParsedTerms:
        system, user, selected, reference_chars, prompt_chars = (
            self._render_bounded_prompt(source_text, target_text, existing)
        )
        try:
            data = self._ask_strict_json(system, user)
        except Exception as exc:
            raise GlossaryExtractionError("model_or_json_error") from exc

        if not isinstance(data, dict):
            raise GlossaryExtractionError("response_not_object")
        if "terms" not in data:
            raise GlossaryExtractionError("terms_missing")
        if set(data) != {"terms"}:
            raise GlossaryExtractionError("response_extra_keys")
        raw = data["terms"]
        if not isinstance(raw, list):
            raise GlossaryExtractionError("terms_not_list")

        terms: list[GlossaryTerm] = []
        normalized = 0
        for item in raw:
            if not isinstance(item, dict):
                continue
            term = GlossaryTerm.from_mapping(item, confidence="medium")
            if term is None:
                continue
            normalized += int(_was_normalized(item, term))
            terms.append(term)

        return _ParsedTerms(
            terms=terms,
            stats={
                "received": len(raw),
                "accepted": len(terms),
                "rejected": len(raw) - len(terms),
                "normalized": normalized,
                "reference_terms_total": (
                    len(existing) if reference_total is None else reference_total
                ),
                "reference_terms_relevant": len(existing),
                "reference_terms_selected": len(selected),
                "reference_terms_dropped": len(existing) - len(selected),
                "reference_chars": reference_chars,
                "prompt_chars": prompt_chars,
            },
        )

    def extract(
        self,
        source_text: str,
        target_text: str,
        existing: list[GlossaryTerm],
    ) -> list[GlossaryTerm]:
        """抽取并清洗术语；保留原有 list 返回契约供独立调用。"""
        return self._extract_with_stats(source_text, target_text, existing).terms

    def extract_and_store(
        self,
        store: GlossaryStore,
        source_text: str,
        target_text: str,
        chapter: int,
        *,
        checkpoint: GlossaryCheckpoint | None = None,
    ) -> dict[str, int]:
        """抽取相关术语，并把本次写入与可选检查点原子提交。"""
        empty_counts = {
            "inserted": 0,
            "updated": 0,
            "conflict": 0,
            "unchanged": 0,
        }
        try:
            all_terms = store.all_terms()
            existing = GlossaryStore.terms_in(all_terms, source_text)
        except Exception as exc:
            raise GlossaryPersistenceError(empty_counts.copy()) from exc

        parsed = self._extract_with_stats(
            source_text,
            target_text,
            existing,
            reference_total=len(all_terms),
        )
        summary = {**empty_counts, **parsed.stats}
        try:
            counts = store.upsert_terms(
                parsed.terms,
                chapter=chapter,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            raise GlossaryPersistenceError(summary) from exc
        summary.update(counts)
        return summary
