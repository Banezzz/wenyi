"""全局分析 Agent（强档）。

通读样章，产出风格指南、角色圣经（含性别/语气）、初始术语候选，
并把角色/术语种入术语库，作为全书翻译的统一基准。
"""

from __future__ import annotations

from typing import Any

from ..glossary.store import GlossaryStore, GlossaryTerm, TYPE_PERSON
from . import prompts
from .base import Agent


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class Analyzer(Agent):
    def analyze(self, sample_text: str) -> dict[str, Any]:
        system = prompts.render("analyzer_system", src=self.src, tgt=self.tgt)
        user = prompts.render("analyzer_user", src=self.src, tgt=self.tgt,
                              sample=sample_text)
        # 不传 default：分析失败照常抛出，由调用方决定（prepare 阶段失败应显式暴露）
        data = self._ask_json(system, user, tier="strong")
        if not isinstance(data, dict):
            data = {}
        for key in (
            "content_type", "genre", "tone", "style_guide", "narration",
            "pacing", "register", "dialogue_style", "rhetoric",
        ):
            data[key] = _text(data.get(key))
        data["characters"] = self.dict_items(data.get("characters"))
        data["terms"] = self.dict_items(data.get("terms"))
        return data

    def seed_glossary(self, store: GlossaryStore, analysis: dict[str, Any]) -> int:
        """把分析得到的角色/术语种入术语库，返回写入条目数。"""
        terms: list[GlossaryTerm] = []
        for ch in self.dict_items(analysis.get("characters")):
            term = GlossaryTerm.from_mapping(
                ch,
                type_override=TYPE_PERSON,
                confidence="medium",
                first_chapter=0,
            )
            if term is None:
                continue
            terms.append(term)
        for tm in self.dict_items(analysis.get("terms")):
            term = GlossaryTerm.from_mapping(
                tm,
                confidence="medium",
                first_chapter=0,
            )
            if term is None:
                continue
            terms.append(term)
        summary = store.upsert_terms(terms, chapter=0, checkpoint=None)
        return sum(summary.values())

    def style_brief(self, analysis: dict[str, Any]) -> str:
        """把分析结果浓缩成给译者注入的风格/角色简报。"""
        lines = []
        for key, tag in (
            ("content_type", "文本类型"), ("genre", "体裁"),
            ("tone", "语气文体"), ("style_guide", "风格指南"),
        ):
            value = _text(analysis.get(key))
            if value:
                lines.append(f"{tag}：{value}")
        # 细粒度风格维度（旧 analysis.json 缺字段时自动跳过，向后兼容）
        for key, tag in (("narration", "叙事"), ("pacing", "句式节奏"),
                         ("register", "语域"), ("dialogue_style", "对话风格"),
                         ("rhetoric", "修辞")):
            value = _text(analysis.get(key))
            if value:
                lines.append(f"{tag}：{value}")
        chars = self.dict_items(analysis.get("characters"))
        if chars:
            lines.append("角色：")
            for c in chars:
                term = GlossaryTerm.from_mapping(c, type_override=TYPE_PERSON)
                if term is None:
                    continue
                gender = f"，{term.gender}" if term.gender else ""
                note = f"，{term.note}" if term.note else ""
                lines.append(
                    f"  - {term.target}({term.source}{gender}{note})"
                )
        return "\n".join(lines)
