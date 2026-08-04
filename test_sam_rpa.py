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
    PauseController,
    load_queue,
    main,
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

    def test_load_queue_reports_control_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            # Literal newline inside a JSON string → Invalid control character
            path.write_text(
                '{\n  "items": [{\n    "id": "pair-004",\n'
                '    "master": {"full_name": "Bad\nName"}\n  }]\n}\n',
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                load_queue(path)
            self.assertIn("Invalid JSON", str(ctx.exception))
            self.assertIn("line", str(ctx.exception))

    def test_page_looks_missing_detects_not_found_text(self):
        from sam_rpa_local import _page_looks_missing
        from unittest.mock import MagicMock

        page = MagicMock()
        page.url = "https://example.test/SAM/Ch/Ch_M_Vw.aspx?chmid=11008"
        page.title.return_value = "SAM"
        page.locator.return_value.inner_text.return_value = "Birth Mother Not Found"
        self.assertEqual(_page_looks_missing(page), "birth_mother_not_found")

    def test_save_confirm_js_is_nonblocking(self):
        # Guardrail: Save/Yes must be scheduled via setTimeout so CDP cannot hang
        # on a synchronous window.confirm() inside evaluate().
        self.assertIn("setTimeout", _SAVE_SCHEDULE_JS)
        self.assertIn("confirm", _SAVE_SCHEDULE_JS)
        self.assertIn("setTimeout", _YES_SCHEDULE_JS)

    def test_id_filter_selects_queue_item(self):
        # --id filters by queue row id; report order is irrelevant because each
        # pair carries its own master/duplicate profile ids.
        from unittest.mock import MagicMock, patch

        queue = {
            "items": [
                {
                    "id": "pair-001",
                    "decision": "approved",
                    "match_reason": "name",
                    "master": {"birth_mother_id": "1"},
                    "duplicate": {"birth_mother_id": "2"},
                },
                {
                    "id": "pair-003",
                    "decision": "approved",
                    "match_reason": "name",
                    "master": {"birth_mother_id": "5564"},
                    "duplicate": {"birth_mother_id": "10522"},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(queue), encoding="utf-8")
            captured: list[str] = []

            def fake_run(page, item, *, dry_run, all_pages=None):
                captured.append(item["id"])
                return {"id": item["id"], "status": "dry_run_ok", "ok": True, "log": []}

            fake_page = MagicMock()
            fake_page.url = "https://example.test/SAM/Ch/Ch_M_Vw.aspx?chmid=10522"
            fake_context = MagicMock()
            fake_context.pages = [fake_page]
            fake_browser = MagicMock()
            fake_browser.contexts = [fake_context]

            with patch("sam_rpa_local.connect_browser", return_value=(MagicMock(), fake_browser)):
                with patch("sam_rpa_local.run_one_merge", side_effect=fake_run):
                    rc = main(
                        [
                            "--queue",
                            str(path),
                            "--dry-run",
                            "--id",
                            "pair-003",
                            "--output-dir",
                            str(Path(tmp) / "out"),
                        ]
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(captured, ["pair-003"])

    def test_offset_and_limit_slice_queue(self):
        from unittest.mock import MagicMock, patch

        queue = {
            "items": [
                {
                    "id": f"pair-{i:03d}",
                    "decision": "approved",
                    "match_reason": "name",
                    "master": {"birth_mother_id": str(i)},
                    "duplicate": {"birth_mother_id": str(1000 + i)},
                }
                for i in range(1, 6)
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text(json.dumps(queue), encoding="utf-8")
            captured: list[str] = []

            def fake_run(page, item, *, dry_run, all_pages=None):
                captured.append(item["id"])
                return {"id": item["id"], "status": "dry_run_ok", "ok": True, "log": []}

            fake_page = MagicMock()
            fake_page.url = "https://example.test/SAM/Ch/Ch_M_Vw.aspx?chmid=1"
            fake_context = MagicMock()
            fake_context.pages = [fake_page]
            fake_browser = MagicMock()
            fake_browser.contexts = [fake_context]

            with patch("sam_rpa_local.connect_browser", return_value=(MagicMock(), fake_browser)):
                with patch("sam_rpa_local.run_one_merge", side_effect=fake_run):
                    with patch("sam_rpa_local.pick_live_page", return_value=fake_page):
                        rc = main(
                            [
                                "--queue",
                                str(path),
                                "--dry-run",
                                "--offset",
                                "2",
                                "--limit",
                                "2",
                                "--output-dir",
                                str(Path(tmp) / "out"),
                            ]
                        )
            self.assertEqual(rc, 0)
            self.assertEqual(captured, ["pair-003", "pair-004"])

    def test_pause_controller_enter_toggles(self):
        pause = PauseController()
        pause._on_enter()
        self.assertTrue(pause._pause_after_current)
        # Cancel pending pause
        pause._on_enter()
        self.assertFalse(pause._pause_after_current)
        # Request again and simulate checkpoint reaching pause
        pause._on_enter()
        with pause._lock:
            pause._paused = True
            pause._pause_after_current = False
        pause._on_enter()
        self.assertFalse(pause._paused)


if __name__ == "__main__":
    unittest.main()
