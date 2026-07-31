"""Tests for SAM local RPA playbook helpers (no browser required)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.sam_playbook import choose_master, should_skip_item
from sam_rpa_local import (
    _SAVE_SCHEDULE_JS,
    _YES_SCHEDULE_JS,
    load_queue,
    normalize_item,
)


class SamPlaybookTests(unittest.TestCase):
    def test_prefers_sf_migration_master(self):
        left = {"birth_mother_id": "100", "sf_migration_contact_id": "SF1", "has_notes": False}
        right = {"birth_mother_id": "200", "sf_migration_contact_id": None, "has_notes": False}
        choice = choose_master(left, right)
        self.assertEqual(choice["master"]["birth_mother_id"], "100")
        self.assertIsNone(choice["skip_reason"])

    def test_dual_sf_prefers_lower_id(self):
        left = {"birth_mother_id": "1300", "sf_migration_contact_id": "A", "has_children": False}
        right = {"birth_mother_id": "803", "sf_migration_contact_id": "B", "has_children": True}
        choice = choose_master(left, right)
        self.assertEqual(choice["master"]["birth_mother_id"], "803")

    def test_notes_conflict_skips(self):
        left = {"birth_mother_id": "1", "has_notes": True, "sf_migration_contact_id": "A"}
        right = {"birth_mother_id": "2", "has_notes": True, "sf_migration_contact_id": None}
        choice = choose_master(left, right)
        self.assertEqual(choice["skip_reason"], "both_profiles_have_notes")

    def test_should_skip_phone_and_unapproved(self):
        self.assertEqual(
            should_skip_item({"decision": "review_needed", "master": {"birth_mother_id": "1"}, "duplicate": {"birth_mother_id": "2"}}),
            "decision_not_approved:review_needed",
        )
        self.assertEqual(
            should_skip_item(
                {
                    "decision": "approved",
                    "match_reason": "phone",
                    "master": {"birth_mother_id": "1"},
                    "duplicate": {"birth_mother_id": "2"},
                }
            ),
            "phone_matches_are_manual_in_v1",
        )

    def test_example_queue_loads(self):
        items = load_queue(Path("samples/sam_merge_queue.example.json"))
        self.assertEqual(len(items), 1)
        normalized = normalize_item(items[0])
        self.assertEqual(normalized["master"]["birth_mother_id"], "803")
        self.assertIsNone(should_skip_item(normalized))

    def test_save_confirm_js_is_nonblocking(self):
        # Guardrail: Save/Yes must be scheduled via setTimeout so CDP cannot hang
        # on a synchronous window.confirm() inside evaluate().
        self.assertIn("setTimeout", _SAVE_SCHEDULE_JS)
        self.assertIn("confirm", _SAVE_SCHEDULE_JS)
        self.assertIn("setTimeout", _YES_SCHEDULE_JS)


if __name__ == "__main__":
    unittest.main()
