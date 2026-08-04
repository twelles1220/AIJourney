"""Tests for CSV → SAM merge queue conversion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sam_queue_from_csv import (
    _ALIASES,
    _FLAT_ALIASES,
    _clean_text,
    _map_headers,
    build_queue,
    flat_rows_to_items,
    rows_to_items,
)


class SamQueueFromCsvTests(unittest.TestCase):
    def test_clean_text_strips_newlines(self):
        self.assertEqual(_clean_text("Jamie Connelly\n"), "Jamie Connelly")

    def test_example_csv_builds_approved_name_pairs(self):
        queue, stats, mapping, mode = build_queue(
            Path("samples/sam_duplicates.example.csv"),
            approve_name=True,
            start_index=6,
        )
        self.assertEqual(mode, "paired")
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
        mapping = _map_headers(headers, _ALIASES)
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

    def test_flat_name_groups(self):
        headers = [
            "Birth Mother » Birth Mother ID",
            "Birth Mother » Birth Mother Full Name",
            "Birth Mother » SF Migration Contact ID BM",
            "Birth Mother Case » Case #",
            "Birth Mother » Alerts",
            "Birth Mother Case » Child Date of Birth",
            "Birth Mother » All Phone Numbers",
        ]
        mapping = _map_headers(headers, _FLAT_ALIASES)
        self.assertEqual(mapping["id"], "Birth Mother » Birth Mother ID")
        rows = [
            {
                "Birth Mother » Birth Mother ID": "100",
                "Birth Mother » Birth Mother Full Name": "Ada Lovelace\n",
                "Birth Mother » SF Migration Contact ID BM": "SF1",
                "Birth Mother Case » Case #": "1",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
            {
                "Birth Mother » Birth Mother ID": "200",
                "Birth Mother » Birth Mother Full Name": "Ada Lovelace",
                "Birth Mother » SF Migration Contact ID BM": "",
                "Birth Mother Case » Case #": "1",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
            {
                "Birth Mother » Birth Mother ID": "300",
                "Birth Mother » Birth Mother Full Name": "Solo Person",
                "Birth Mother » SF Migration Contact ID BM": "",
                "Birth Mother Case » Case #": "",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
            {
                "Birth Mother » Birth Mother ID": "401",
                "Birth Mother » Birth Mother Full Name": "Triple Name",
                "Birth Mother » SF Migration Contact ID BM": "SF",
                "Birth Mother Case » Case #": "1",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
            {
                "Birth Mother » Birth Mother ID": "402",
                "Birth Mother » Birth Mother Full Name": "Triple Name",
                "Birth Mother » SF Migration Contact ID BM": "",
                "Birth Mother Case » Case #": "",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
            {
                "Birth Mother » Birth Mother ID": "403",
                "Birth Mother » Birth Mother Full Name": "Triple Name",
                "Birth Mother » SF Migration Contact ID BM": "",
                "Birth Mother Case » Case #": "",
                "Birth Mother » Alerts": "",
                "Birth Mother Case » Child Date of Birth": "",
                "Birth Mother » All Phone Numbers": "",
            },
        ]
        items, stats = flat_rows_to_items(
            rows, mapping, approve_name=True, approve_multi=False
        )
        self.assertEqual(stats["name_groups_2"], 1)
        self.assertEqual(stats["name_groups_3plus"], 1)
        self.assertEqual(stats["approved_name"], 1)
        self.assertEqual(stats["review_needed"], 2)  # two dups in triple group
        approved = [i for i in items if i["decision"] == "approved"][0]
        self.assertEqual(approved["master"]["birth_mother_id"], "100")
        self.assertEqual(approved["duplicate"]["birth_mother_id"], "200")
        self.assertEqual(approved["master"]["full_name"], "Ada Lovelace")

    def test_flat_utf16_roundtrip_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flat.csv"
            content = (
                '"Birth Mother » Birth Mother ID","Birth Mother » Birth Mother Full Name",'
                '"Birth Mother » SF Migration Contact ID BM"\n'
                '10,"Jamie Test",SF1\n'
                '20,"Jamie Test",\n'
            )
            path.write_bytes(content.encode("utf-16-le"))
            queue, stats, mapping, mode = build_queue(path, approve_name=True)
            self.assertEqual(mode, "flat_name")
            self.assertEqual(stats["approved_name"], 1)
            self.assertEqual(queue["items"][0]["master"]["birth_mother_id"], "10")

    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path("samples/sam_duplicates.example.csv")
            queue, _, _, _ = build_queue(src, approve_name=True)
            out = Path(tmp) / "q.json"
            out.write_text(json.dumps(queue), encoding="utf-8")
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(loaded["items"]), 3)


if __name__ == "__main__":
    unittest.main()
