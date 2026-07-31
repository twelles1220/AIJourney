"""Tests for CSV → SAM merge queue conversion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sam_queue_from_csv import build_queue, rows_to_items, _map_headers, _clean_text


class SamQueueFromCsvTests(unittest.TestCase):
    def test_clean_text_strips_newlines(self):
        self.assertEqual(_clean_text("Jamie Connelly\n"), "Jamie Connelly")

    def test_example_csv_builds_approved_name_pairs(self):
        queue, stats, mapping = build_queue(
            Path("samples/sam_duplicates.example.csv"),
            approve_name=True,
            start_index=6,
        )
        self.assertIn("left_id", mapping)
        self.assertEqual(stats["approved_name"], 2)
        self.assertEqual(stats["phone_manual"], 1)
        approved = [i for i in queue["items"] if i["decision"] == "approved"]
        self.assertEqual(len(approved), 2)
        self.assertEqual(approved[0]["id"], "pair-006")
        self.assertEqual(approved[0]["master"]["birth_mother_id"], "3288")
        self.assertEqual(approved[0]["duplicate"]["birth_mother_id"], "11075")
        phone = [i for i in queue["items"] if i["match_reason"] == "phone"][0]
        self.assertEqual(phone["decision"], "review_needed")

    def test_sf_prefers_master(self):
        headers = [
            "birth_mother_id_1",
            "name_1",
            "sf_id_1",
            "birth_mother_id_2",
            "name_2",
            "sf_id_2",
            "match_type",
        ]
        mapping = _map_headers(headers)
        rows = [
            {
                "birth_mother_id_1": "1300",
                "name_1": "A",
                "sf_id_1": "",
                "birth_mother_id_2": "803",
                "name_2": "A",
                "sf_id_2": "SF-9",
                "match_type": "name",
            }
        ]
        items, stats = rows_to_items(rows, mapping, approve_name=True)
        self.assertEqual(stats["approved_name"], 1)
        self.assertEqual(items[0]["master"]["birth_mother_id"], "803")
        self.assertEqual(items[0]["duplicate"]["birth_mother_id"], "1300")

    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path("samples/sam_duplicates.example.csv")
            queue, _, _ = build_queue(src, approve_name=True)
            out = Path(tmp) / "q.json"
            out.write_text(json.dumps(queue), encoding="utf-8")
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(loaded["items"]), 3)


if __name__ == "__main__":
    unittest.main()
