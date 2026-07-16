"""Bounded chapter-glossary plans and resumable v2 reconciliation."""

from __future__ import annotations

from typing import Any, Callable

from ..glossary.store import GlossaryCheckpoint, GlossaryStore
from .runstore import RunStore, source_fingerprint, translation_fingerprint

CHAPTER_GLOSSARY_PLAN_VERSION = 1
CHAPTER_GLOSSARY_MAX_UNITS = 3
CHAPTER_GLOSSARY_MAX_SOURCE_CHARS = 6_000

ExtractGlossaryFn = Callable[..., dict[str, int] | None]


def batch_glossary_checkpoints(
    chapter: int,
    text_segs,
    units: list[dict],
) -> list[GlossaryCheckpoint]:
    return [
        GlossaryCheckpoint(
            scope="batch",
            chapter=chapter,
            start_index=unit["start_index"],
            count=unit["count"],
            fingerprint=translation_fingerprint(
                text_segs[
                    unit["start_index"]:
                    unit["start_index"] + unit["count"]
                ]
            ),
        )
        for unit in units
    ]


def expected_chapter_glossary_plan(text_segs, units: list[dict]) -> dict:
    windows: list[dict[str, Any]] = []
    start_unit = 0
    while start_unit < len(units):
        chosen_end = start_unit
        for end_unit in range(
            start_unit + 1,
            min(start_unit + CHAPTER_GLOSSARY_MAX_UNITS, len(units)) + 1,
        ):
            first = units[start_unit]["start_index"]
            last = units[end_unit - 1]
            end = last["start_index"] + last["count"]
            source_chars = len("\n".join(
                segment.source for segment in text_segs[first:end]
            ))
            if source_chars > CHAPTER_GLOSSARY_MAX_SOURCE_CHARS:
                break
            chosen_end = end_unit

        if chosen_end == start_unit:
            raise ValueError("canonical unit exceeds chapter window source limit")
        if chosen_end - start_unit == 1 and chosen_end < len(units):
            raise ValueError("cannot preserve overlap within chapter window limit")

        first = units[start_unit]["start_index"]
        last = units[chosen_end - 1]
        end = last["start_index"] + last["count"]
        windows.append({
            "start_unit": start_unit,
            "unit_count": chosen_end - start_unit,
            "start_index": first,
            "count": end - first,
            "source_fingerprint": source_fingerprint([
                segment.source for segment in text_segs[first:end]
            ]),
        })
        if chosen_end == len(units):
            break
        start_unit = chosen_end - 1

    identity = [
        "chapter-glossary-plan",
        str(CHAPTER_GLOSSARY_PLAN_VERSION),
        str(CHAPTER_GLOSSARY_MAX_UNITS),
        str(CHAPTER_GLOSSARY_MAX_SOURCE_CHARS),
    ]
    for window in windows:
        identity.extend(str(window[key]) for key in (
            "start_unit",
            "unit_count",
            "start_index",
            "count",
            "source_fingerprint",
        ))
    return {
        "version": CHAPTER_GLOSSARY_PLAN_VERSION,
        "max_units": CHAPTER_GLOSSARY_MAX_UNITS,
        "max_source_chars": CHAPTER_GLOSSARY_MAX_SOURCE_CHARS,
        "fingerprint": source_fingerprint(identity),
        "windows": windows,
    }


def chapter_glossary_windows(
    chapter,
    text_segs,
    units: list[dict],
    store: RunStore,
    *,
    create: bool,
) -> dict | None:
    has_plan = "chapter_glossary_plan" in chapter.meta
    try:
        expected = expected_chapter_glossary_plan(text_segs, units)
        if not has_plan:
            if not create:
                return None
            chapter.meta["chapter_glossary_plan"] = expected
            store.save_chapter(chapter)
            store.log_event(
                "chapter_glossary_plan_created",
                chapter=chapter.index,
                version=expected["version"],
                fingerprint=expected["fingerprint"],
                window_count=len(expected["windows"]),
                max_source_chars=expected["max_source_chars"],
            )
            return expected
        if chapter.meta.get("chapter_glossary_plan") != expected:
            raise ValueError("persisted chapter window plan does not match sources")
        return expected
    except (TypeError, ValueError) as exc:
        store.log_event(
            "chapter_glossary_plan_invalid",
            chapter=chapter.index,
            error=str(exc),
        )
        raise RuntimeError(
            f"第 {chapter.index} 章章级术语窗口计划与正文不一致；"
            "为保护已有译文，已停止续跑。"
        ) from exc


def chapter_glossary_window_checkpoints(
    chapter: int,
    text_segs,
    plan: dict,
) -> list[GlossaryCheckpoint]:
    return [
        GlossaryCheckpoint(
            scope="chapter_window",
            chapter=chapter,
            start_index=window["start_index"],
            count=window["count"],
            fingerprint=translation_fingerprint(
                text_segs[
                    window["start_index"]:
                    window["start_index"] + window["count"]
                ]
            ),
            plan_fingerprint=plan["fingerprint"],
        )
        for window in plan["windows"]
    ]


def reconcile_chapter_glossary(
    *,
    glossary: GlossaryStore,
    store: RunStore,
    chapter,
    text_segs,
    units: list[dict],
    extract_glossary: ExtractGlossaryFn,
) -> tuple[bool, bool]:
    """Complete the direct v1 path or the bounded, resumable v2 path."""
    chapter_index = chapter.index
    batches = batch_glossary_checkpoints(chapter_index, text_segs, units)
    has_window_plan = "chapter_glossary_plan" in chapter.meta
    direct_parent = GlossaryCheckpoint(
        scope="chapter",
        chapter=chapter_index,
        start_index=0,
        count=len(text_segs),
        fingerprint=translation_fingerprint(text_segs),
    )
    if not has_window_plan and glossary.checkpoint_matches(direct_parent):
        complete = all(glossary.checkpoint_matches(cp) for cp in batches)
        store.log_event(
            "chapter_glossary_skipped",
            chapter=chapter_index,
            fingerprint=direct_parent.fingerprint,
            checkpoint_version=direct_parent.version,
            reason="checkpoint_match",
            completed=complete,
        )
        return complete, False

    plan = chapter_glossary_windows(
        chapter,
        text_segs,
        units,
        store,
        create=True,
    )
    assert plan is not None
    windows = chapter_glossary_window_checkpoints(
        chapter_index, text_segs, plan
    )
    if glossary.chapter_completion_matches_v2(
        chapter=chapter_index,
        plan_fingerprint=plan["fingerprint"],
        batch_checkpoints=batches,
        window_checkpoints=windows,
    ):
        parent = glossary.build_derived_chapter_checkpoint(
            chapter=chapter_index,
            plan_fingerprint=plan["fingerprint"],
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        store.log_event(
            "chapter_glossary_skipped",
            chapter=chapter_index,
            fingerprint=parent.fingerprint,
            checkpoint_version=parent.version,
            protocol_version=2,
            plan_fingerprint=plan["fingerprint"],
            reason="checkpoint_match",
            completed=True,
        )
        return True, False

    changed = False
    for checkpoint in windows:
        if glossary.checkpoint_matches(checkpoint):
            store.log_event(
                "chapter_glossary_window_skipped",
                chapter=chapter_index,
                start_index=checkpoint.start_index,
                count=checkpoint.count,
                fingerprint=checkpoint.fingerprint,
                checkpoint_version=checkpoint.version,
                plan_fingerprint=plan["fingerprint"],
                reason="checkpoint_match",
            )
            continue
        segments = text_segs[
            checkpoint.start_index:
            checkpoint.start_index + checkpoint.count
        ]
        summary = extract_glossary(
            glossary,
            store,
            chapter_index,
            segments,
            phase="chapter_window",
            checkpoint=checkpoint,
        )
        changed |= summary is not None

    parent = glossary.derive_chapter_checkpoint(
        chapter=chapter_index,
        plan_fingerprint=plan["fingerprint"],
        batch_checkpoints=batches,
        window_checkpoints=windows,
    )
    if parent is None:
        store.log_event(
            "chapter_glossary_derivation_deferred",
            chapter=chapter_index,
            plan_fingerprint=plan["fingerprint"],
            batch_count=len(batches),
            window_count=len(windows),
            completed=False,
        )
        return False, changed

    store.log_event(
        "chapter_glossary_derived",
        chapter=chapter_index,
        fingerprint=parent.fingerprint,
        checkpoint_version=parent.version,
        protocol_version=2,
        plan_fingerprint=plan["fingerprint"],
        batch_count=len(batches),
        window_count=len(windows),
        completed=True,
        generation_id=glossary.generation_id,
    )
    return True, changed
