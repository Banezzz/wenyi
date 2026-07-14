"""术语库 + 翻译记忆库测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace

from trans_novel.glossary.store import (
    GlossaryCheckpoint,
    GlossaryStore,
    GlossaryStoreIdentityError,
    GlossaryTerm,
    TYPE_APPELLATION,
    TYPE_PERSON,
    TYPE_TERM,
)


class TestGlossary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self.tmp.name, "g.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_insert_and_lookup(self):
        r = self.store.upsert_term(
            GlossaryTerm(
                source="綾小路",
                target="绫小路",
                type=TYPE_PERSON,
                gender="男",
                aliases=["綾小路くん"],
                reading="あやのこうじ",
            ),
            chapter=0,
        )
        self.assertEqual(r, "inserted")
        t = self.store.get_term("綾小路")
        assert t is not None
        self.assertEqual(t.target, "绫小路")
        self.assertEqual(t.gender, "男")

    def test_terms_in_text_matches_alias(self):
        self.store.upsert_term(
            GlossaryTerm(source="綾小路", target="绫小路", aliases=["綾小路くん"])
        )
        hits = self.store.terms_in_text("「おはよう、綾小路くん」と堀北が言った。")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "綾小路")

    def test_single_han_term_does_not_match_inside_word(self):
        self.store.upsert_term(GlossaryTerm(source="李", target="李", type=TYPE_PERSON))
        self.assertEqual(self.store.terms_in_text("行李放在门口。"), [])
        hits = self.store.terms_in_text("李さんは窓の外を見た。")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "李")

    def test_appellation_does_not_match_bare_name_alias(self):
        self.store.upsert_term(
            GlossaryTerm(
                source="夏帆ちゃん",
                target="小夏帆",
                type=TYPE_APPELLATION,
                aliases=["夏帆"],
            )
        )
        self.assertEqual(self.store.terms_in_text("夏帆は窓の外を見た。"), [])
        hits = self.store.terms_in_text("「夏帆ちゃん」と母親が言った。")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source, "夏帆ちゃん")

    def test_conflict_keeps_locked(self):
        self.store.upsert_term(
            GlossaryTerm(source="堀北", target="堀北", confidence="high"), chapter=0
        )
        self.store.lock_term("堀北")
        # 提出不同译法 → 应保留锁定译法并记冲突
        r = self.store.upsert_term(
            GlossaryTerm(source="堀北", target="掘北", confidence="medium"), chapter=1
        )
        self.assertEqual(r, "conflict")
        term = self.store.get_term("堀北")
        assert term is not None
        self.assertEqual(term.target, "堀北")
        self.assertEqual(len(self.store.open_conflicts()), 1)

    def test_conflict_overrides_low_confidence(self):
        self.store.upsert_term(
            GlossaryTerm(source="X", target="旧译", confidence="low"), chapter=0
        )
        r = self.store.upsert_term(
            GlossaryTerm(source="X", target="新译", confidence="high"), chapter=1
        )
        self.assertEqual(r, "updated")
        term = self.store.get_term("X")
        assert term is not None
        self.assertEqual(term.target, "新译")

    def test_from_mapping_normalizes_untrusted_values(self):
        term = GlossaryTerm.from_mapping(
            {
                "source": "  source  ",
                "target": "  target  ",
                "reading": ["not-bindable"],
                "type": {"not": "a string"},
                "gender": " Unknown ",
                "aliases": [" first ", "", None, "first", "second", 7],
                "note": 7,
            },
            first_chapter=4,
        )
        assert term is not None
        self.assertEqual(term.source, "source")
        self.assertEqual(term.target, "target")
        self.assertEqual(term.reading, "")
        self.assertEqual(term.type, TYPE_TERM)
        self.assertEqual(term.gender, "")
        self.assertEqual(term.aliases, ["first", "second"])
        self.assertEqual(term.note, "")
        self.assertEqual(term.first_chapter, 4)

        person = GlossaryTerm.from_mapping(
            {"source": "A", "target": "甲", "type": "组织", "gender": "未知"},
            type_override=TYPE_PERSON,
        )
        assert person is not None
        self.assertEqual(person.type, TYPE_PERSON)
        self.assertEqual(person.gender, "")

    def test_from_mapping_rejects_invalid_required_values(self):
        invalid_values = (None, 7, ["value"], {"value": "x"}, "   ")
        for value in invalid_values:
            with self.subTest(source=value):
                self.assertIsNone(
                    GlossaryTerm.from_mapping({"source": value, "target": "valid"})
                )
            with self.subTest(target=value):
                self.assertIsNone(
                    GlossaryTerm.from_mapping({"source": "valid", "target": value})
                )

    def test_from_row_normalizes_legacy_gender_and_aliases(self):
        self.store.conn.execute(
            """INSERT INTO glossary
               (source,target,type,gender,aliases,locked,status,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                "legacy",
                "旧译",
                None,
                "未知",
                json.dumps([" alias ", 7, "alias", "second"]),
                0,
                None,
                0.0,
            ),
        )
        self.store.conn.commit()

        term = self.store.get_term("legacy")
        assert term is not None
        self.assertEqual(term.type, TYPE_TERM)
        self.assertEqual(term.gender, "")
        self.assertEqual(term.aliases, ["alias", "second"])
        self.assertEqual(term.status, "ok")

    def test_upsert_rejects_non_bindable_types_before_sqlite(self):
        invalid_terms = [
            ("type", GlossaryTerm(source="A", target="甲", type=["术语"])),
            ("reading", GlossaryTerm(source="B", target="乙", reading={"x": 1})),
            ("aliases", GlossaryTerm(source="C", target="丙", aliases=["ok", 7])),
            ("first_chapter", GlossaryTerm(source="D", target="丁", first_chapter=[])),
        ]
        for field_name, term in invalid_terms:
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(TypeError, field_name):
                    self.store.upsert_term(term, chapter=1)
        with self.assertRaisesRegex(TypeError, "chapter"):
            self.store.upsert_term(GlossaryTerm(source="E", target="戊"), chapter=[])
        self.assertEqual(self.store.stats()["terms"], 0)

    def test_bulk_upsert_prevalidates_every_term(self):
        checkpoint = GlossaryCheckpoint("batch", 2, 10, 2, "batch-fingerprint")
        valid = GlossaryTerm(source="valid", target="有效")
        invalid = GlossaryTerm(source="invalid", target="无效", note=["bad"])

        with self.assertRaisesRegex(TypeError, "note"):
            self.store.upsert_terms([valid, invalid], chapter=2, checkpoint=checkpoint)
        self.assertIsNone(self.store.get_term("valid"))
        self.assertFalse(self.store.checkpoint_matches(checkpoint))

    def test_checkpoint_update_and_generation_persistence(self):
        checkpoint = GlossaryCheckpoint("batch", 3, 5, 2, "fingerprint-a")
        summary = self.store.upsert_terms(
            [GlossaryTerm(source="A", target="甲")],
            chapter=3,
            checkpoint=checkpoint,
        )
        self.assertEqual(
            summary,
            {"inserted": 1, "updated": 0, "conflict": 0, "unchanged": 0},
        )
        self.assertTrue(self.store.checkpoint_matches(checkpoint))

        replacement = GlossaryCheckpoint("batch", 3, 5, 3, "fingerprint-b", version=2)
        self.store.record_checkpoint(replacement)
        self.assertFalse(self.store.checkpoint_matches(checkpoint))
        self.assertTrue(self.store.checkpoint_matches(replacement))
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM glossary_extraction_checkpoints"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        generation_id = self.store.generation_id
        db_path = self.store.db_path
        self.store.close()
        self.store = GlossaryStore(db_path)
        self.assertEqual(self.store.generation_id, generation_id)
        self.assertTrue(self.store.checkpoint_matches(replacement))

    def test_opening_legacy_database_adds_window_table_without_data_loss(self):
        checkpoint = GlossaryCheckpoint("batch", 3, 0, 1, "legacy-batch")
        self.store.upsert_terms(
            [GlossaryTerm(source="legacy", target="旧译")],
            chapter=3,
            checkpoint=checkpoint,
        )
        generation_id = self.store.generation_id
        db_path = self.store.db_path
        self.store.conn.execute(
            "DROP TABLE IF EXISTS glossary_chapter_window_checkpoints"
        )
        self.store.conn.commit()
        self.store.close()

        self.store = GlossaryStore(
            db_path,
            expected_generation_id=generation_id,
        )

        self.assertEqual(self.store.generation_id, generation_id)
        self.assertEqual(self.store.get_term("legacy").target, "旧译")
        self.assertTrue(self.store.checkpoint_matches(checkpoint))
        table = self.store.conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='glossary_chapter_window_checkpoints'"""
        ).fetchone()
        self.assertIsNotNone(table)

        window = GlossaryCheckpoint(
            "chapter_window",
            3,
            0,
            1,
            "window-fingerprint",
            plan_fingerprint="plan-v1",
        )
        self.store.record_checkpoint(window)
        self.assertTrue(self.store.checkpoint_matches(window))

    def test_empty_window_result_still_records_checkpoint(self):
        checkpoint = GlossaryCheckpoint(
            "chapter_window",
            4,
            0,
            2,
            "empty-window",
            plan_fingerprint="plan-v1",
        )

        summary = self.store.upsert_terms(
            [],
            chapter=4,
            checkpoint=checkpoint,
        )

        self.assertEqual(
            summary,
            {"inserted": 0, "updated": 0, "conflict": 0, "unchanged": 0},
        )
        self.assertTrue(self.store.checkpoint_matches(checkpoint))

    def test_window_upsert_rolls_back_terms_conflicts_and_checkpoint(self):
        self.store.upsert_term(
            GlossaryTerm(source="locked", target="canonical", confidence="high"),
            chapter=0,
        )
        self.store.lock_term("locked")
        checkpoint = GlossaryCheckpoint(
            "chapter_window",
            4,
            0,
            2,
            "rollback-window",
            plan_fingerprint="plan-v1",
        )
        self.store.conn.execute(
            """CREATE TRIGGER fail_window_checkpoint BEFORE INSERT
               ON glossary_chapter_window_checkpoints
               BEGIN SELECT RAISE(ABORT, 'window checkpoint failure'); END"""
        )
        self.store.conn.commit()

        with self.assertRaisesRegex(Exception, "window checkpoint failure"):
            self.store.upsert_terms(
                [
                    GlossaryTerm(source="locked", target="proposal"),
                    GlossaryTerm(source="new", target="新译"),
                ],
                chapter=4,
                checkpoint=checkpoint,
            )

        locked = self.store.get_term("locked")
        assert locked is not None
        self.assertEqual(locked.target, "canonical")
        self.assertEqual(locked.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])
        self.assertIsNone(self.store.get_term("new"))
        self.assertFalse(self.store.checkpoint_matches(checkpoint))

    @staticmethod
    def _v2_checkpoint_set():
        chapter = 7
        plan_fingerprint = "chapter-window-plan-v1"
        batches = [
            GlossaryCheckpoint("batch", chapter, 0, 2, "batch-a"),
            GlossaryCheckpoint("batch", chapter, 2, 2, "batch-b"),
        ]
        windows = [
            GlossaryCheckpoint(
                "chapter_window",
                chapter,
                0,
                3,
                "window-a",
                plan_fingerprint=plan_fingerprint,
            ),
            GlossaryCheckpoint(
                "chapter_window",
                chapter,
                1,
                3,
                "window-b",
                plan_fingerprint=plan_fingerprint,
            ),
        ]
        return chapter, plan_fingerprint, batches, windows

    def test_derive_chapter_checkpoint_requires_every_child(self):
        chapter, plan_fingerprint, batches, windows = self._v2_checkpoint_set()
        for checkpoint in [*batches, windows[0]]:
            self.store.record_checkpoint(checkpoint)

        expected_parent = self.store.build_derived_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        self.assertEqual(expected_parent.scope, "chapter")
        self.assertEqual(expected_parent.start_index, 0)
        self.assertEqual(expected_parent.count, 4)
        self.assertEqual(expected_parent.version, 2)
        self.assertEqual(expected_parent.plan_fingerprint, plan_fingerprint)

        self.assertIsNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        self.assertFalse(self.store.checkpoint_matches(expected_parent))

        self.store.record_checkpoint(windows[1])
        derived = self.store.derive_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        self.assertEqual(derived, expected_parent)
        self.assertTrue(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )

    def test_derived_checkpoint_rejects_malformed_or_extra_children(self):
        chapter, plan_fingerprint, batches, windows = self._v2_checkpoint_set()
        malformed = [
            ("reordered-batches", list(reversed(batches)), windows),
            ("reordered-windows", batches, list(reversed(windows))),
            ("duplicate-window", batches, [windows[0], windows[0], windows[1]]),
            (
                "wrong-plan",
                batches,
                [
                    replace(windows[0], plan_fingerprint="another-plan"),
                    windows[1],
                ],
            ),
        ]
        for label, candidate_batches, candidate_windows in malformed:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.store.build_derived_chapter_checkpoint(
                        chapter=chapter,
                        plan_fingerprint=plan_fingerprint,
                        batch_checkpoints=candidate_batches,
                        window_checkpoints=candidate_windows,
                    )

        for checkpoint in [*batches, *windows]:
            self.store.record_checkpoint(checkpoint)
        extra = GlossaryCheckpoint("batch", chapter, 4, 1, "unexpected-batch")
        self.store.record_checkpoint(extra)
        self.assertIsNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        self.assertFalse(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )

        self.store.conn.execute(
            """DELETE FROM glossary_extraction_checkpoints
               WHERE scope='batch' AND chapter=? AND start_index=?""",
            (chapter, extra.start_index),
        )
        self.store.conn.commit()

        extra_window = GlossaryCheckpoint(
            "chapter_window",
            chapter,
            2,
            2,
            "unexpected-window",
            plan_fingerprint=plan_fingerprint,
        )
        self.store.record_checkpoint(extra_window)
        self.assertIsNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        self.store.conn.execute(
            """DELETE FROM glossary_chapter_window_checkpoints
               WHERE chapter=? AND start_index=?""",
            (chapter, extra_window.start_index),
        )
        self.store.conn.commit()

        stale_plan_window = replace(
            extra_window,
            fingerprint="stale-plan-window",
            plan_fingerprint="stale-plan",
        )
        self.store.record_checkpoint(stale_plan_window)
        self.assertIsNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        self.assertFalse(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        self.store.conn.execute(
            """DELETE FROM glossary_chapter_window_checkpoints
               WHERE chapter=? AND start_index=?""",
            (chapter, stale_plan_window.start_index),
        )
        self.store.conn.commit()
        self.assertIsNotNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )

    def test_derive_chapter_checkpoint_rejects_stale_child_generation(self):
        chapter, plan_fingerprint, batches, windows = self._v2_checkpoint_set()
        for checkpoint in [*batches, *windows]:
            self.store.record_checkpoint(checkpoint)
        self.store.conn.execute(
            """UPDATE glossary_chapter_window_checkpoints
               SET generation_id='stale-generation'
               WHERE chapter=? AND start_index=?""",
            (chapter, windows[0].start_index),
        )
        self.store.conn.commit()

        self.assertIsNone(
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )
        expected_parent = self.store.build_derived_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        self.assertFalse(self.store.checkpoint_matches(expected_parent))

    def test_derived_checkpoint_operations_reject_nested_transactions(self):
        chapter, plan_fingerprint, batches, windows = self._v2_checkpoint_set()
        for checkpoint in [*batches, *windows]:
            self.store.record_checkpoint(checkpoint)

        self.store.conn.execute("BEGIN")
        with self.assertRaisesRegex(RuntimeError, "active transaction"):
            self.store.derive_chapter_checkpoint(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        self.assertTrue(self.store.conn.in_transaction)
        with self.assertRaisesRegex(RuntimeError, "active transaction"):
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()

        parent = self.store.build_derived_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        self.assertFalse(self.store.checkpoint_matches(parent))

    def test_v2_completion_rechecks_children_when_parent_remains(self):
        chapter, plan_fingerprint, batches, windows = self._v2_checkpoint_set()
        for checkpoint in [*batches, *windows]:
            self.store.record_checkpoint(checkpoint)
        parent = self.store.derive_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=windows,
        )
        assert parent is not None
        self.assertTrue(self.store.checkpoint_matches(parent))

        self.store.conn.execute(
            """DELETE FROM glossary_chapter_window_checkpoints
               WHERE chapter=? AND start_index=?""",
            (chapter, windows[0].start_index),
        )
        self.store.conn.commit()
        self.assertTrue(self.store.checkpoint_matches(parent))
        self.assertFalse(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )

        self.store.record_checkpoint(windows[0])
        self.assertTrue(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=windows,
            )
        )

        changed_windows = [
            replace(windows[0], fingerprint="window-a-after-target-change"),
            windows[1],
        ]
        changed_parent = self.store.build_derived_chapter_checkpoint(
            chapter=chapter,
            plan_fingerprint=plan_fingerprint,
            batch_checkpoints=batches,
            window_checkpoints=changed_windows,
        )
        self.assertNotEqual(changed_parent.fingerprint, parent.fingerprint)
        self.assertTrue(self.store.checkpoint_matches(parent))
        self.assertFalse(
            self.store.chapter_completion_matches_v2(
                chapter=chapter,
                plan_fingerprint=plan_fingerprint,
                batch_checkpoints=batches,
                window_checkpoints=changed_windows,
            )
        )

    def test_existing_store_open_fails_closed_on_missing_or_wrong_generation(self):
        db_path = self.store.db_path
        generation_id = self.store.generation_id
        self.store.close()

        reopened = GlossaryStore(
            db_path,
            create=False,
            expected_generation_id=generation_id,
        )
        reopened.close()

        with self.assertRaisesRegex(GlossaryStoreIdentityError, "mismatch"):
            GlossaryStore(
                db_path,
                create=False,
                expected_generation_id="not-the-owned-generation",
            )

        os.remove(db_path)
        with self.assertRaisesRegex(FileNotFoundError, "missing"):
            GlossaryStore(db_path, create=False)
        self.assertFalse(os.path.exists(db_path))

        # tearDown expects an open store; use an unrelated temporary database.
        self.store = GlossaryStore(db_path)

    def test_bulk_upsert_rolls_back_terms_conflicts_and_checkpoint(self):
        self.store.upsert_term(
            GlossaryTerm(source="locked", target="canonical", confidence="high"),
            chapter=0,
        )
        self.store.lock_term("locked")
        checkpoint = GlossaryCheckpoint("batch", 4, 0, 2, "rollback-test")
        self.store.conn.execute(
            """CREATE TRIGGER fail_checkpoint BEFORE INSERT
               ON glossary_extraction_checkpoints
               BEGIN SELECT RAISE(ABORT, 'checkpoint failure'); END"""
        )
        self.store.conn.commit()

        with self.assertRaisesRegex(Exception, "checkpoint failure"):
            self.store.upsert_terms(
                [
                    GlossaryTerm(source="locked", target="proposal"),
                    GlossaryTerm(source="new", target="新译"),
                ],
                chapter=4,
                checkpoint=checkpoint,
            )

        locked = self.store.get_term("locked")
        assert locked is not None
        self.assertEqual(locked.target, "canonical")
        self.assertEqual(locked.status, "ok")
        self.assertEqual(self.store.open_conflicts(), [])
        self.assertIsNone(self.store.get_term("new"))
        self.assertFalse(self.store.checkpoint_matches(checkpoint))

    def test_translation_memory(self):
        self.store.add_tm("風が強かった。", "风很大。", chapter=1)
        self.assertEqual(self.store.tm_lookup("風が強かった。"), "风很大。")
        self.assertIsNone(self.store.tm_lookup("未登録"))

    def test_stats(self):
        self.store.upsert_term(GlossaryTerm(source="A", target="甲"))
        self.store.add_tm("a", "甲译")
        s = self.store.stats()
        self.assertEqual(s["terms"], 1)
        self.assertEqual(s["tm_entries"], 1)


if __name__ == "__main__":
    unittest.main()
