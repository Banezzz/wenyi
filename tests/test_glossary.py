"""术语库 + 翻译记忆库测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

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
            GlossaryTerm(source="綾小路", target="绫小路", type=TYPE_PERSON,
                         gender="男", aliases=["綾小路くん"], reading="あやのこうじ"),
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
            self.store.upsert_terms(
                [valid, invalid], chapter=2, checkpoint=checkpoint
            )
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
