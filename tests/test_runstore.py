"""RunStore fingerprint and legacy-event regression tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from trans_novel.pipeline.runstore import (
    RunStore,
    source_fingerprint,
    translation_fingerprint,
)


class TestRunStoreFingerprints(unittest.TestCase):
    def test_source_fingerprint_frames_each_value(self):
        self.assertNotEqual(
            source_fingerprint(["ab", "c"]),
            source_fingerprint(["a", "bc"]),
        )

    def test_translation_fingerprint_frames_source_and_target(self):
        self.assertNotEqual(
            translation_fingerprint([{"source": "ab", "target": "c"}]),
            translation_fingerprint([{"source": "a", "target": "bc"}]),
        )


class TestLegacyGlossaryEvidence(unittest.TestCase):
    _ZERO = {
        "inserted": 0,
        "updated": 0,
        "conflict": 0,
        "unchanged": 0,
    }

    @staticmethod
    def _segments(chapter: int) -> list[dict]:
        return [{
            "index": 0,
            "source": f"source-{chapter}",
            "target": f"target-{chapter}",
        }]

    @classmethod
    def _log_translation(cls, store: RunStore, chapter: int) -> list[dict]:
        segments = cls._segments(chapter)
        store.log_event(
            "batch_translated",
            chapter=chapter,
            start_index=0,
            count=1,
            segments=segments,
        )
        return segments

    def test_legacy_reader_ignores_zero_malformed_and_new_events(self):
        invalid_summaries = [
            self._ZERO,
            {"inserted": 1, "updated": 0, "conflict": 0},
            {**self._ZERO, "inserted": "1"},
            {**self._ZERO, "inserted": True},
            {**self._ZERO, "inserted": -1},
            [],
        ]

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "run"))
            for chapter, summary in enumerate(invalid_summaries):
                self._log_translation(store, chapter)
                store.log_event(
                    "batch_glossary_extracted",
                    chapter=chapter,
                    start_index=0,
                    count=1,
                    summary=summary,
                )

            # A current-format event must be left to the SQLite checkpoint path.
            current_chapter = len(invalid_summaries)
            self._log_translation(store, current_chapter)
            store.log_event(
                "batch_glossary_extracted",
                chapter=current_chapter,
                start_index=0,
                count=1,
                summary={**self._ZERO, "inserted": 1},
                checkpoint_version=1,
            )

            # Nonzero evidence without a matching translation event is not exact.
            unmatched_chapter = current_chapter + 1
            store.log_event(
                "batch_glossary_extracted",
                chapter=unmatched_chapter,
                start_index=0,
                count=1,
                summary={**self._ZERO, "inserted": 1},
            )

            valid_chapter = unmatched_chapter + 1
            valid_segments = self._log_translation(store, valid_chapter)
            store.log_event(
                "batch_glossary_extracted",
                chapter=valid_chapter,
                start_index=0,
                count=1,
                summary={**self._ZERO, "unchanged": 1},
            )
            with open(store.event_log_path, "a", encoding="utf-8") as f:
                f.write("{malformed json\n")

            self.assertEqual(
                store.legacy_batch_glossary_evidence(),
                {
                    (valid_chapter, 0, 1): translation_fingerprint(valid_segments),
                },
            )


if __name__ == "__main__":
    unittest.main()
